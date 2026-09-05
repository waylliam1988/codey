"""Live A/B for a forced Research follow-up coverage gap.

The initial Research iteration is scripted so both arms start from the same
clean-but-incomplete report. The planner arm then uses the production
``ResearchPipeline`` selection, ``PlanExecutor``, evidence-only follow-up, and
deterministic merge path. The fixture search provider reveals the hidden source
only after the initial iteration has returned, which keeps the gap deterministic
without monkeypatching production planner decisions.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.knowledge.store import KnowledgeStore
from codey.providers import controls as provider_controls
from codey.providers.registry import connect_provider, provider_ids
from codey.research.context import ResearchContext, ResearchPipelineConfig
from codey.research.evidence_followup import run_evidence_followup
from codey.research.evidence_ledger import EvidenceLedgerStore
from codey.research.followup_quality import followup_usefulness, score_followup_quality_row
from codey.research.pipeline import ResearchIterationRun, ResearchPipeline
from codey.research.proof_quality import review_research_proof
from codey.research.runner import ResearchRunner
from tests.manual.ab_harness_common import (
    AB_FAILURE_CODEY,
    AB_FAILURE_NONE,
    AB_FAILURE_PROVIDER,
    ABJournalWriter,
    ArmRunLayout,
    FixtureDocument,
    ResultRowStore,
    TRANSCRIPT_MODE_FLAGS,
    TracingProvider,
    attach_research_record_payload,
    bind_row_evidence_refs,
    build_arm_manifest,
    fixture_network_policy_bypass,
    timestamp,
    write_arm_manifest,
)
from tests.manual.ab_journal import ABJournalIdentityMismatch, TranscriptReplayCache


PROBE = "research_forced_followup_gap_ab"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
WEB_PROVIDERS = tuple(provider_id for provider_id in provider_ids() if provider_id != "local")
ARMS = ("baseline", "planner")


@dataclass(frozen=True)
class Case:
    name: str
    question: str
    documents: tuple[FixtureDocument, ...]
    expected_terms: tuple[str, ...]


CASES = {
    "warehouse_quoted_gap": Case(
        name="warehouse_quoted_gap",
        question=(
            'Research the warehouse retrofit fixture. Answer with evidence for '
            '"peak demand charges" and "fire-code setbacks".'
        ),
        documents=(
            FixtureDocument(
                "https://source-a.test/warehouse-peak-demand",
                "Warehouse peak demand source",
                (
                    "Lithium storage retrofits can reduce peak demand charges in "
                    "small warehouses with predictable loads."
                ),
                keywords=("peak", "demand", "charges", "warehouse", "retrofit", "lithium"),
                default=True,
            ),
            FixtureDocument(
                "https://source-b.test/warehouse-fire-code-setbacks",
                "Warehouse fire-code setback source",
                (
                    "Fire-code setbacks are the main limitation for lithium storage "
                    "retrofits because they may require a separated battery room and "
                    "ventilation upgrades."
                ),
                keywords=("fire-code", "fire", "setbacks", "limitation", "warehouse", "retrofit", "lithium"),
            ),
        ),
        expected_terms=("peak demand charges", "fire-code setbacks"),
    )
}


class TwoPhaseFixtureSearchProvider:
    name = "two-phase-fixture-search"

    def __init__(self, documents: Sequence[FixtureDocument]) -> None:
        self.documents = tuple(documents)
        self.queries: list[str] = []
        self.fetches: list[str] = []
        self.followup_enabled = False
        self.closed = False

    def enable_followup_material(self) -> None:
        self.followup_enabled = True

    def search(self, query: str, limit: int = 8) -> list[dict[str, str]]:
        self.queries.append(str(query or ""))
        docs = self._visible_documents(str(query or ""))
        return [doc.result() for doc in docs[: max(0, int(limit or 0))]]

    def fetch(self, url: str) -> dict[str, object]:
        self.fetches.append(str(url or ""))
        for doc in self.documents:
            if doc.url == url:
                return {
                    "url": doc.url,
                    "title": doc.title,
                    "text": doc.text,
                    "truncated": False,
                }
        return {"url": url, "title": "", "text": "ERROR: fixture URL not found", "truncated": False}

    def close(self) -> None:
        self.closed = True

    def _visible_documents(self, query: str) -> list[FixtureDocument]:
        defaults = [doc for doc in self.documents if doc.default]
        if not self.followup_enabled:
            return defaults
        lower = query.casefold()
        matches = [
            doc
            for doc in self.documents
            if doc.keywords and any(keyword.casefold() in lower for keyword in doc.keywords)
        ]
        ordered: list[FixtureDocument] = []
        for doc in [*matches, *defaults]:
            if doc not in ordered:
                ordered.append(doc)
        return ordered


class ScriptedThenLiveProvider:
    def __init__(
        self,
        provider,
        *,
        scripted_replies: Sequence[str],
        send_timeout: float,
        new_chat_timeout: float,
    ) -> None:
        self.provider = provider
        self.scripted_replies = list(scripted_replies)
        self.scripted_send_count = 0
        self.live_send_count = 0
        self.send_timeout = max(1.0, float(send_timeout))
        self.new_chat_timeout = max(1.0, float(new_chat_timeout))
        self.name = getattr(provider, "name", "")
        self.location = getattr(provider, "location", "")
        self.id = getattr(provider, "id", "")
        self.thread_safe_send = getattr(provider, "thread_safe_send", False)

    def __getattr__(self, name: str):
        return getattr(self.provider, name)

    def new_chat(self, timeout=None):
        effective = self.new_chat_timeout if timeout is None else timeout
        return self.provider.new_chat(timeout=effective)

    def send(self, text: str, timeout=None) -> str:
        if self.scripted_replies:
            self.scripted_send_count += 1
            return self.scripted_replies.pop(0)
        self.live_send_count += 1
        effective = self.send_timeout if timeout is None else timeout
        return self.provider.send(text, timeout=effective)

    def close(self) -> None:
        closer = getattr(self.provider, "close", None)
        if callable(closer):
            closer()


def _config_for_arm(arm: str) -> ResearchPipelineConfig:
    if arm == "baseline":
        return ResearchPipelineConfig(enabled=False, max_followup_rounds=0)
    return ResearchPipelineConfig(
        enabled=True,
        max_followup_rounds=1,
        max_queries_per_round=3,
        max_sources_per_query=2,
        max_total_sources=4,
        max_source_preview_chars=2400,
        max_followup_context_chars=8000,
    )


def _initial_replies(case: Case) -> tuple[str, ...]:
    first = case.documents[0]
    answer = (
        "## 结论\n"
        "- Lithium storage retrofits can reduce peak demand charges in small warehouses. [1]\n\n"
        "## 关键证据\n"
        "- [1] Lithium storage retrofits can reduce peak demand charges in small warehouses with predictable loads.\n\n"
        "## 反证与限制\n"
        "- 未找到强反证；初始材料只覆盖一个用户指定检查点，另一个检查点仍需补充来源确认。\n\n"
        "## 来源质量\n"
        "- [1] secondary · web · undated · source-a.test\n\n"
        "## 搜索覆盖\n"
        "- query: warehouse retrofit peak demand charges\n"
        "- opened: Warehouse peak demand source\n"
        "- skipped: fixture hides follow-up material during initial Research\n"
        "- stop: enough to produce a clean but intentionally incomplete baseline\n\n"
        "## 来源\n"
        f"[1] Warehouse peak demand source - {first.url}"
    )
    return (
        json.dumps({"tool": "web_search", "args": {"query": "warehouse retrofit peak demand charges"}}),
        json.dumps({"tool": "open_result", "args": {"result_id": "r1"}}),
        json.dumps(
            {
                "tool": "knowledge_write",
                "args": {
                    "type": "fact",
                    "title": "Warehouse peak demand evidence",
                    "body": "Lithium storage retrofits can reduce peak demand charges in small warehouses.",
                    "sources": ["s1"],
                    "evidence": [
                        {
                            "claim": "Lithium storage retrofits can reduce peak demand charges in small warehouses.",
                            "source_url": "s1",
                            "excerpt": (
                                "Lithium storage retrofits can reduce peak demand charges in "
                                "small warehouses with predictable loads."
                            ),
                            "stance": "supports",
                        }
                    ],
                },
            }
        ),
        json.dumps({"tool": "done", "args": {"answer": answer}}, ensure_ascii=False),
    )


def run_case(
    provider,
    *,
    provider_id: str,
    case: Case,
    arm: str,
    max_turns: int,
    run_id: str,
    trace: ABJournalWriter | None,
    layout: ArmRunLayout,
    send_timeout: float,
    new_chat_timeout: float,
) -> dict[str, Any]:
    started = time.time()
    model_actions: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    infos: list[str] = []
    with tempfile.TemporaryDirectory(prefix="codey-research-forced-followup-gap-ab-") as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        evidence_ledgers = EvidenceLedgerStore(root / "state")
        search = TwoPhaseFixtureSearchProvider(case.documents)
        hybrid = ScriptedThenLiveProvider(
            provider,
            scripted_replies=_initial_replies(case),
            send_timeout=send_timeout,
            new_chat_timeout=new_chat_timeout,
        )
        run_provider = TracingProvider(
            hybrid,
            journal=trace,
            case=case.name,
            arm=arm,
            timeout=send_timeout,
            new_chat_timeout=new_chat_timeout,
        )

        def run_iteration(
            *,
            task: str,
            max_turns: int,
            chat_handoff: str,
            search: object,
            tools=None,
            iteration_context: str = "",
            topic_continuity_context: str = "",
            topic_continuity_payload: Mapping[str, object] | None = None,
        ) -> ResearchIterationRun:
            runner = ResearchRunner(
                run_provider,
                search,
                store,
                max_turns=max_turns,
                session_id=f"{PROBE}-{provider_id}-{case.name}-{arm}",
                project="",
                run_id=run_id,
                chat_handoff=chat_handoff,
                tools=tools,
                iteration_context=iteration_context,
                topic_continuity_context=topic_continuity_context,
                topic_continuity_payload=topic_continuity_payload,
            )
            for event in runner.run(task):
                if event.kind == "turn":
                    model_actions.extend(_safe_model_actions(event.turn, event.reply))
                if event.kind == "info":
                    infos.append(_clip(event.message, 240))
                if event.kind == "tool" and event.call is not None:
                    args = event.call.args if isinstance(event.call.args, dict) else {}
                    tool_calls.append(
                        {
                            "turn": event.turn,
                            "name": event.call.name,
                            "args": _safe_args(args),
                            "ok": bool(event.outcome.ok) if event.outcome is not None else False,
                            "status": event.outcome.presentation_status() if event.outcome is not None else "",
                        }
                    )
            search.enable_followup_material()
            if runner.result is None:
                raise RuntimeError("research finished without result")
            return ResearchIterationRun(result=runner.result, tools=runner.tools)

        def run_followup(
            *,
            tools,
            plan,
            material,
            question: str,
            initial_summary: str = "",
            max_context_chars: int = 8000,
            should_stop=None,
        ):
            before_turn = run_provider.send_index
            result = run_evidence_followup(
                provider=run_provider,
                tools=tools,
                plan=plan,
                material=material,
                question=question,
                initial_summary=initial_summary,
                max_context_chars=max_context_chars,
                should_stop=should_stop,
            )
            if run_provider.send_index > before_turn:
                actions = _safe_model_actions(run_provider.last_turn, run_provider.last_reply)
                model_actions.extend(actions)
                for action in actions or [{"tool": "", "args": {}}]:
                    tool_calls.append(
                        {
                            "turn": run_provider.last_turn,
                            "name": str(action.get("tool") or ""),
                            "args": action.get("args", {}) if isinstance(action.get("args"), dict) else {},
                            "ok": bool(result.ok),
                            "status": result.stop_reason or ("ok" if result.ok else "error"),
                        }
                    )
            return result

        try:
            if trace is not None:
                trace.record_case_start(case=case.name, arm=arm, question_chars=len(case.question))
            context = ResearchContext(
                question=case.question,
                session_id=f"{PROBE}-{provider_id}-{case.name}-{arm}",
                run_id=run_id,
                project="",
                provider_id=provider_id,
                max_turns=max_turns,
            )
            pipeline = ResearchPipeline(
                context=context,
                run_iteration=run_iteration,
                search_factory=lambda: search,
                evidence_followup_runner=run_followup,
                evidence_ledgers=evidence_ledgers,
                config=_config_for_arm(arm),
            )
            pipeline_result = pipeline.run()
            result = pipeline_result.final_result
            proof = (
                review_research_proof(result.research_record, question=case.question)
                if result.research_record is not None
                else None
            )
            row = {
                "provider": provider_id,
                "case": case.name,
                "arm": arm,
                "ok": True,
                "seconds": round(time.time() - started, 3),
                "stop_reason": result.stop_reason,
                "turns": result.turns,
                "max_turns_used": int(getattr(result, "max_turns_used", 0) or 0),
                "sources_read": result.sources_read,
                "opened_urls": result.source_urls[:12],
                "evidence_count": len(result.evidence_items),
                "proof_ok": bool(proof.ok) if proof is not None else False,
                "proof_answer_status": proof.answer_status if proof is not None else "",
                "proof_coverage": proof.answer_coverage_score if proof is not None else None,
                "proof_missing_evidence": list(proof.missing_evidence[:8]) if proof is not None else [],
                "citation_locator_verified": bool(proof.citation_locator_verified) if proof is not None else False,
                "support_relation_verified": bool(proof.support_relation_verified) if proof is not None else False,
                "counterevidence_checked": bool(proof.counterevidence_checked) if proof is not None else False,
                "expected_terms_present": _expected_terms_present(result.summary, case.expected_terms),
                "followup_applied": pipeline_result.followup_applied,
                "followup_rounds": pipeline_result.followup_rounds,
                "pipeline_stop_reason": pipeline_result.stop_reason,
                "planner_stop_reason": pipeline_result.planner_stop_reason,
                "fresh_source_count": pipeline_result.fresh_source_count,
                "new_evidence_count": pipeline_result.new_evidence_count,
                "attempted_fresh_source_count": pipeline_result.attempted_fresh_source_count,
                "attempted_new_evidence_count": pipeline_result.attempted_new_evidence_count,
                "ab_followup_mode": (
                    "forced_gap_production_evidence_followup"
                    if arm == "planner" and pipeline_result.followup_rounds
                    else ""
                ),
                "provider_send_count": run_provider.send_index,
                "provider_reply_count": run_provider.reply_count,
                "provider_prompt_chars": run_provider.prompt_chars,
                "provider_reply_chars": run_provider.reply_chars,
                "scripted_initial_send_count": hybrid.scripted_send_count,
                "live_followup_send_count": hybrid.live_send_count,
                "fixture_queries": search.queries[:12],
                "fixture_fetches": search.fetches[:12],
                "model_actions": model_actions[:40],
                "tool_calls": tool_calls[:40],
                "info": infos[:16],
                "summary_chars": len(result.summary or ""),
                "summary_preview": _clip(result.summary, 1200),
            }
            if result.research_record is not None:
                summary = result.research_record.to_summary_payload()
                row.update(
                    {
                        "record_source_count": int(summary.get("source_count") or 0),
                        "record_evidence_count": int(summary.get("evidence_count") or 0),
                        "record_claim_count": int(summary.get("claim_count") or 0),
                        "unsupported_claim_count": int(summary.get("unsupported_claim_count") or 0),
                    }
                )
                claims = max(1, int(row["record_claim_count"] or 0))
                row["unsupported_claim_rate"] = round(int(row["unsupported_claim_count"] or 0) / claims, 3)
            attach_research_record_payload(row, result.research_record)
            row["score"] = score_followup_quality_row(row)
            bind_row_evidence_refs(row, layout=layout, tracing_provider=run_provider)
            if trace is not None:
                trace.record_case_complete(case=case.name, arm=arm, row=row)
            return row
        except Exception as exc:
            row = {
                "provider": provider_id,
                "case": case.name,
                "arm": arm,
                "ok": False,
                "seconds": round(time.time() - started, 3),
                "error": f"{type(exc).__name__}: {exc}",
                "codey_failure_class": AB_FAILURE_CODEY,
                "provider_send_count": run_provider.send_index,
                "provider_reply_count": run_provider.reply_count,
                "provider_prompt_chars": run_provider.prompt_chars,
                "provider_reply_chars": run_provider.reply_chars,
                "scripted_initial_send_count": hybrid.scripted_send_count,
                "live_followup_send_count": hybrid.live_send_count,
                "fixture_queries": search.queries[:12],
                "fixture_fetches": search.fetches[:12],
                "model_actions": model_actions[:40],
                "tool_calls": tool_calls[:40],
                "info": infos[:16],
            }
            bind_row_evidence_refs(row, layout=layout, tracing_provider=run_provider)
            if trace is not None:
                trace.record_case_complete(case=case.name, arm=arm, row=row)
            return row
        finally:
            store.close()


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"rows": len(rows), "by_case": {}}
    for case in sorted({str(row.get("case") or "") for row in rows if row.get("case")}):
        case_rows = [row for row in rows if row.get("case") == case]
        arms = {str(row.get("arm") or ""): row for row in case_rows if row.get("ok")}
        baseline = arms.get("baseline", {})
        planner = arms.get("planner", {})
        usefulness = followup_usefulness(baseline, planner)
        summary["by_case"][case] = {
            "baseline_score": baseline.get("score"),
            "planner_score": planner.get("score"),
            "delta": (
                int(planner.get("score") or 0) - int(baseline.get("score") or 0)
                if baseline and planner
                else None
            ),
            "baseline_proof_ok": baseline.get("proof_ok"),
            "planner_proof_ok": planner.get("proof_ok"),
            "planner_followup_rounds": planner.get("followup_rounds"),
            "planner_new_evidence_count": planner.get("new_evidence_count"),
            "planner_stop_reason": planner.get("planner_stop_reason"),
            "followup_usefulness": usefulness,
        }
    return summary


def gate_ok(rows: list[dict[str, Any]], complete: bool) -> bool:
    if not complete:
        return False
    arms = {str(row.get("arm") or ""): row for row in rows if row.get("ok")}
    baseline = arms.get("baseline")
    planner = arms.get("planner")
    if not baseline or not planner:
        return False
    return bool(
        planner.get("proof_ok")
        and not baseline.get("proof_ok")
        and int(planner.get("followup_rounds") or 0) >= 1
        and int(planner.get("new_evidence_count") or 0) > 0
        and int(planner.get("score") or 0) > int(baseline.get("score") or 0)
    )


def run_provider(
    provider_id: str,
    *,
    cases: tuple[Case, ...],
    arms: tuple[str, ...],
    port: int,
    output: Path,
    max_turns: int,
    send_timeout: float,
    new_chat_timeout: float,
    open_if_missing: bool,
    rerun_failed: bool,
    trace: ABJournalWriter | None,
    layout: ArmRunLayout,
) -> dict[str, Any]:
    store = ResultRowStore.open(
        output,
        probe=PROBE,
        provider_id=provider_id,
        cases=cases,
        arms=arms,
        summarize=summarize,
        ok=gate_ok,
    )
    if trace is not None:
        store.payload["trace_output"] = str(trace.directory)
    pending = [
        (case_name, arm)
        for case_name, arm, _repeat in store.pending_keys(cases=cases, arms=arms, rerun_failed=rerun_failed)
    ]
    if trace is not None:
        trace.record_run_start(
            cases=tuple(case.name for case in cases),
            arms=arms,
            max_turns=max_turns,
            resumed_attempt=trace.event_count > 0,
        )
    if not pending:
        store.write(complete=True, extra={"finished_at": timestamp(), "stop_reason": "already_complete"})
        return store.payload
    store.write(complete=False)
    provider_controls.begin_task_context(f"{PROBE}:{provider_id}")
    provider = None
    try:
        with fixture_network_policy_bypass():
            provider = connect_provider(
                provider_id,
                port=port,
                open_if_missing=open_if_missing,
                bring_to_front=open_if_missing,
            )
            pending_set = set(pending)
            for case in cases:
                for arm in arms:
                    if (case.name, arm) not in pending_set:
                        continue
                    row = run_case(
                        provider,
                        provider_id=provider_id,
                        case=case,
                        arm=arm,
                        max_turns=max_turns,
                        run_id=output.stem,
                        trace=trace,
                        layout=layout,
                        send_timeout=send_timeout,
                        new_chat_timeout=new_chat_timeout,
                    )
                    store.upsert(row, complete=False)
                    print(
                        f"[{provider_id} {case.name} {arm}] "
                        f"ok={row.get('ok')} score={row.get('score')} "
                        f"proof={row.get('proof_ok')} followup={row.get('followup_rounds')} "
                        f"new_ev={row.get('new_evidence_count')} "
                        f"stop={row.get('planner_stop_reason') or row.get('stop_reason', row.get('error', ''))}",
                        flush=True,
                    )
            store.write(complete=True, extra={"finished_at": timestamp(), "stop_reason": "done"})
            if trace is not None:
                trace.record_run_complete(rows=len(store.rows))
            return store.payload
    finally:
        provider_controls.end_task_context()
        if provider is not None:
            closer = getattr(provider, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass


def _safe_model_actions(turn: int, reply: str) -> list[dict[str, Any]]:
    from codey.research.protocols import extract_json_objects

    actions: list[dict[str, Any]] = []
    for obj in extract_json_objects(reply or ""):
        tool = str(obj.get("tool") or "").strip().lower()
        args = obj.get("args")
        actions.append({"turn": int(turn or 0), "tool": _clip(tool, 80), "args": _safe_args(args if isinstance(args, dict) else {})})
    return actions[:3]


def _safe_args(args: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in args.items():
        if key in {"query", "url", "source_id", "result_id", "type", "title"}:
            safe[str(key)] = _clip(value, 240)
        elif key in {"sources", "evidence"}:
            safe[str(key)] = _shape(value)
    return safe


def _shape(value: object) -> object:
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        return "object"
    return _clip(value, 120)


def _expected_terms_present(summary: str, expected_terms: Sequence[str]) -> bool:
    haystack = " ".join(str(summary or "").casefold().split())
    return all(" ".join(str(term or "").casefold().split()) in haystack for term in expected_terms)


def _clip(value: object, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if limit <= 3:
        return text[:limit]
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _parse_names(raw: Sequence[str] | None, choices: Mapping[str, Any] | Sequence[str], label: str) -> tuple[str, ...]:
    allowed = set(choices.keys() if isinstance(choices, Mapping) else choices)
    values: list[str] = []
    for item in raw or ():
        values.extend(part.strip() for part in str(item or "").split(",") if part.strip())
    if not values:
        values = sorted(allowed)
    unknown = [item for item in values if item not in allowed]
    if unknown:
        raise SystemExit(f"unknown {label}: {', '.join(unknown)}")
    return tuple(values)


def _transcript_cache_mode(value: str) -> str | None:
    text = str(value or "").strip()
    if text not in TRANSCRIPT_MODE_FLAGS:
        raise SystemExit(f"unknown transcript mode: {text}")
    return TRANSCRIPT_MODE_FLAGS[text]


def _default_output(provider_id: str) -> Path:
    stamp = time.strftime("%Y%m%d")
    return RESULTS_DIR / f"{PROBE}-{provider_id}-{stamp}.json"


def _self_test() -> None:
    search = TwoPhaseFixtureSearchProvider(CASES["warehouse_quoted_gap"].documents)
    assert [row["url"] for row in search.search("fire-code setbacks")] == [
        "https://source-a.test/warehouse-peak-demand"
    ]
    search.enable_followup_material()
    urls = [row["url"] for row in search.search("fire-code setbacks")]
    assert urls[0] == "https://source-b.test/warehouse-fire-code-setbacks"
    assert _expected_terms_present(
        "peak demand charges and fire-code setbacks",
        CASES["warehouse_quoted_gap"].expected_terms,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Live A/B for a forced Research follow-up coverage gap")
    parser.add_argument("--provider", choices=(*WEB_PROVIDERS, "all"), default="mimo")
    parser.add_argument("--case", action="append", help="case name or comma list; defaults to all cases")
    parser.add_argument("--arms", default="baseline,planner")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--trace-output", type=Path)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--send-timeout", type=float, default=180)
    parser.add_argument("--new-chat-timeout", type=float, default=90)
    parser.add_argument("--open-if-missing", action="store_true")
    parser.add_argument("--rerun-failed", action="store_true")
    parser.add_argument("--transcript-mode", choices=("digest-only", "archive", "off"), default="archive")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        _self_test()
        print("self-test ok")
        return 0

    selected_cases = tuple(CASES[name] for name in _parse_names(args.case, CASES, "case"))
    selected_arms = _parse_names([args.arms], ARMS, "arm")
    providers = WEB_PROVIDERS if args.provider == "all" else (args.provider,)
    for provider_id in providers:
        output = args.output or _default_output(provider_id)
        if args.provider == "all" and args.output is not None:
            output = args.output.with_name(f"{args.output.stem}-{provider_id}{args.output.suffix}")
        layout = ArmRunLayout.for_output(output, journal_dir=args.trace_output)
        cache_mode = _transcript_cache_mode(args.transcript_mode)
        trace: ABJournalWriter | None = None
        if cache_mode is not None:
            try:
                trace = ABJournalWriter(
                    directory=layout.journal_dir,
                    experiment_id=PROBE,
                    run_id=output.stem,
                    provider=provider_id,
                    transcript_cache=TranscriptReplayCache(layout.journal_dir, mode=cache_mode),
                )
            except ABJournalIdentityMismatch as exc:
                print(str(exc), file=sys.stderr)
                return 2
        try:
            try:
                payload = run_provider(
                    provider_id,
                    cases=selected_cases,
                    arms=selected_arms,
                    port=args.port,
                    output=output,
                    max_turns=max(1, args.max_turns),
                    send_timeout=args.send_timeout,
                    new_chat_timeout=args.new_chat_timeout,
                    open_if_missing=args.open_if_missing,
                    rerun_failed=args.rerun_failed,
                    trace=trace,
                    layout=layout,
                )
                ok = bool(payload.get("ok"))
                codey_failure_class = AB_FAILURE_NONE if ok else AB_FAILURE_CODEY
                provider_failure_class = AB_FAILURE_NONE
            except Exception as exc:
                print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
                ok = False
                codey_failure_class = AB_FAILURE_CODEY
                provider_failure_class = AB_FAILURE_PROVIDER
                payload = {}
            write_arm_manifest(
                output,
                build_arm_manifest(
                    suite=PROBE,
                    provider=provider_id,
                    arms=selected_arms,
                    cases=[case.name for case in selected_cases],
                    max_turns=max(1, args.max_turns),
                    journal_dir=layout.journal_dir if trace is not None else None,
                    transcript_mode=args.transcript_mode if trace is not None else "off",
                    started_at=str(payload.get("started_at") or ""),
                    finished_at=str(payload.get("finished_at") or timestamp()),
                    stop_reason=str(payload.get("stop_reason") or ("done" if ok else "unknown")),
                    provider_error_class=provider_failure_class,
                    codey_failure_class=codey_failure_class,
                ),
            )
            if not ok:
                return 1
        finally:
            if trace is not None:
                trace.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
