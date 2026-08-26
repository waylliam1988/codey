"""Live A/B probe for the 0.4.4 bounded Research planner.

The baseline arm runs the production ResearchPipeline with follow-up disabled.
The planner arm enables one bounded follow-up round. Both arms use the same
production ResearchRunner and deterministic fixture search provider, while the
web-model provider is live. Progress is written atomically after each row, and
an optional durable observation journal (`<stem>.trace/`) records every
provider send/reply pair as hash-chained JSONL events with digest-only
transcripts by default.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.manual.ab_harness_common import (
    FixtureDocument,
    FixtureSearchProvider,
    OutputProviderMismatch,
    TracingProvider,
    fixture_material_phase,
    fixture_url_policy_bypass,
    journal_directory_for_output,
    load_or_new_payload,
    normalize_payload_metadata,
    timestamp,
    write_payload_bounded,
)
from tests.manual.ab_journal import (
    ABJournalIdentityMismatch,
    ABJournalReader,
    ABJournalWriter,
)
from codey.providers import controls as provider_controls
from codey.knowledge.store import KnowledgeStore
from codey.providers.registry import connect_provider, provider_ids
from codey.research.context import ResearchContext, ResearchPipelineConfig
from codey.research.evidence_ledger import EvidenceLedgerStore
from codey.research.evidence_followup import run_evidence_followup
from codey.research.plan_executor import PlanExecutionResult
from codey.research.pipeline import ResearchIterationRun, ResearchPipeline
from codey.research.proof_quality import review_research_proof
from codey.research.protocols import extract_json_objects
from codey.research.query_planner import QueryCandidate, ResearchPlan
from codey.research.runner import ResearchRunner
from codey.research.tools import ResearchTools, clone_research_tools


RESULTS_DIR = Path(__file__).resolve().parent / "results"
WEB_PROVIDERS = tuple(provider_id for provider_id in provider_ids() if provider_id != "local")
ARMS = ("baseline", "planner")
AB_EVIDENCE_ONLY_FOLLOWUP_MODE = "production_evidence_followup"


@dataclass(frozen=True)
class Case:
    name: str
    question: str
    documents: tuple[FixtureDocument, ...]
    expected_terms: tuple[str, ...]


CASES = {
    "warehouse_gap": Case(
        name="warehouse_gap",
        question=(
            "Research whether lithium storage retrofits are practical for small "
            "warehouses. Include the main benefit and the main limitation."
        ),
        documents=(
            FixtureDocument(
                "https://source-a.test/lithium-storage-benefit",
                "Benchmark source A",
                (
                    "Lithium storage retrofits can reduce peak demand charges in "
                    "small warehouses with predictable loads and daily cycling."
                ),
                keywords=("benefit", "practical", "warehouse", "retrofit", "lithium"),
                default=True,
            ),
            FixtureDocument(
                "https://source-b.test/lithium-storage-limit",
                "Benchmark source B",
                (
                    "The main limitation is that fire-code setbacks may require a "
                    "separated battery room and ventilation retrofit costs can exceed "
                    "the demand-charge savings."
                ),
                keywords=("limitation", "limit", "counter", "fire", "setback", "ventilation"),
            ),
        ),
        expected_terms=("peak demand charges", "fire-code setbacks"),
    ),
    "widget_noop": Case(
        name="widget_noop",
        question=(
            "Research the current Widget Storage API recommendation and cite the "
            "recommended endpoint."
        ),
        documents=(
            FixtureDocument(
                "https://source-a.test/widget-storage",
                "Benchmark source A",
                (
                    "The Widget Storage standard still recommends the stable-v2 "
                    "endpoint for client storage integration."
                ),
                keywords=("widget", "storage", "endpoint", "stable-v2", "recommend"),
                default=True,
            ),
            FixtureDocument(
                "https://source-b.test/widget-storage-update",
                "Benchmark source B",
                (
                    "The Widget Storage working group has not adopted a stable-v3 "
                    "successor; stable-v2 remains the recommended endpoint."
                ),
                keywords=("primary", "source", "evidence", "current"),
            ),
        ),
        expected_terms=("stable-v2",),
    ),
}


class FreshMaterialPlanExecutor:
    """A/B-only executor variant that treats already-opened URLs as non-material."""

    def __init__(self, *, config: ResearchPipelineConfig, should_stop) -> None:
        self.config = config
        self.should_stop = should_stop

    def execute(self, plan: ResearchPlan, tools: ResearchTools) -> PlanExecutionResult:
        runtime = clone_research_tools(tools)
        baseline_opened = _opened_url_set(runtime)
        queries: list[str] = []
        opened: list[dict[str, Any]] = []
        previews: list[str] = []
        fresh_urls: list[str] = []
        skipped = 0
        errors: list[str] = []
        seen = set(baseline_opened)
        query_limit = max(0, min(int(plan.max_queries or 0), int(self.config.max_queries_per_round or 0)))
        total_limit = max(0, int(self.config.max_total_sources or 0))
        per_query_limit = max(0, int(self.config.max_sources_per_query or 0))
        stop_reason = "no_queries"
        for candidate in plan.query_candidates[:query_limit]:
            if self.should_stop():
                stop_reason = "stopped"
                break
            query = " ".join(str(candidate.query_preview or "").split())
            if not query:
                skipped += 1
                continue
            before_searches = len(runtime.ledger.searches)
            with fixture_material_phase(runtime.search):
                result = runtime.web_search(query)
            queries.append(query)
            if str(result or "").startswith("ERROR:"):
                errors.append(_clip(result, 180))
                stop_reason = "search_error"
                continue
            search = runtime.ledger.searches[-1] if len(runtime.ledger.searches) > before_searches else None
            if search is None:
                stop_reason = "no_results"
                continue
            opened_for_query = 0
            for hit in search.results:
                if len(opened) >= total_limit:
                    stop_reason = "max_sources"
                    break
                if opened_for_query >= per_query_limit:
                    break
                url = str(hit.url or "").strip()
                if not url or url in seen or runtime.ledger.canonical_opened_url(url):
                    skipped += 1
                    seen.add(url)
                    continue
                seen.add(url)
                before_opened = _opened_url_set(runtime)
                body = runtime.open_url(url, limit=self.config.max_source_preview_chars)
                text = str(body or "")
                if text.startswith(("ERROR:", "SKIPPED:")):
                    skipped += 1
                    errors.append(_clip(text, 180))
                    continue
                after_opened = _opened_url_set(runtime)
                new_urls = sorted(after_opened - before_opened - baseline_opened)
                if not new_urls:
                    skipped += 1
                    continue
                source = _opened_source_payload(runtime, new_urls[-1])
                if not source:
                    skipped += 1
                    continue
                opened.append(source)
                fresh_urls.append(new_urls[-1])
                previews.append(_source_preview(query, source, text, self.config.max_source_preview_chars))
                opened_for_query += 1
                stop_reason = "opened_sources"
            if stop_reason in {"max_sources", "stopped"}:
                break
        if queries and not opened and stop_reason not in {"search_error", "stopped"}:
            stop_reason = "no_new_material"
        return PlanExecutionResult(
            queries_executed=tuple(queries),
            opened_sources=tuple(opened),
            previews=tuple(previews),
            fresh_source_urls=tuple(fresh_urls),
            fresh_source_count=len(fresh_urls),
            skipped_count=skipped,
            stop_reason=stop_reason,
            errors=tuple(errors[:12]),
        )


def _opened_url_set(tools: ResearchTools) -> set[str]:
    urls = {str(url or "").strip() for url in getattr(tools, "sources_read", set())}
    urls.update(str(url or "").strip() for url in tools.ledger.final_url_set())
    for item in tools.ledger.opened_sources:
        urls.add(str(item.requested_url or "").strip())
        urls.add(str(item.final_url or "").strip())
    return {url for url in urls if url}


def _opened_source_payload(tools: ResearchTools, url: str) -> dict[str, Any]:
    final_url = tools.ledger.canonical_opened_url(url) or str(url or "")
    for item in tools.ledger.opened_sources:
        if item.final_url == final_url or item.requested_url == final_url:
            return item.to_dict()
    return {}


def _source_preview(query: str, source: dict[str, Any], body: str, limit: int) -> str:
    title = str(source.get("title") or "").strip()
    final_url = str(source.get("final_url") or source.get("url") or "").strip()
    header = " | ".join(part for part in (title, final_url) if part)
    text = _clip(body, max(500, min(4000, int(limit or 0))))
    return "\n".join(part for part in (f"query: {query}", header, text) if part)


class TimedProvider:
    def __init__(self, provider, *, send_timeout: float, new_chat_timeout: float) -> None:
        self.provider = provider
        self.send_timeout = max(1.0, float(send_timeout))
        self.new_chat_timeout = max(1.0, float(new_chat_timeout))
        self.name = getattr(provider, "name", "")
        self.location = getattr(provider, "location", "")

    def __getattr__(self, name: str):
        return getattr(self.provider, name)

    def new_chat(self, timeout=None) -> None:
        effective = self.new_chat_timeout if timeout is None else timeout
        return self.provider.new_chat(timeout=effective)

    def send(self, text: str, timeout=None) -> str:
        effective = self.send_timeout if timeout is None else timeout
        return self.provider.send(text, timeout=effective)

    def close(self) -> None:
        return self.provider.close()


def run_case(
    provider,
    *,
    provider_id: str,
    case: Case,
    arm: str,
    max_turns: int,
    run_id: str,
    trace: ABJournalWriter | None,
) -> dict[str, Any]:
    started = time.time()
    model_actions: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    infos: list[str] = []
    with tempfile.TemporaryDirectory(prefix="codey-bounded-planner-ab-") as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        evidence_ledgers = EvidenceLedgerStore(root / "state")
        search = FixtureSearchProvider(case.documents)
        iterations: list[ResearchIterationRun] = []
        if trace is not None:
            trace.record_case_start(
                case=case.name,
                arm=arm,
                question_chars=len(case.question),
            )
        run_provider = TracingProvider(
            provider,
            journal=trace,
            case=case.name,
            arm=arm,
        )

        def run_iteration(
            *,
            task: str,
            max_turns: int,
            chat_handoff: str,
            search: object,
            tools=None,
            iteration_context: str = "",
        ) -> ResearchIterationRun:
            runner = ResearchRunner(
                run_provider,
                search,
                store,
                max_turns=max_turns,
                session_id=f"bounded-planner-ab-{provider_id}-{case.name}-{arm}",
                project="",
                run_id=run_id,
                chat_handoff=chat_handoff,
                tools=tools,
                iteration_context=iteration_context,
            )
            for event in runner.run(task):
                if event.kind == "turn":
                    model_actions.extend(_safe_model_actions(event.turn, event.reply)[:3])
                if event.kind == "info":
                    infos.append(str(event.message or "")[:240])
                if event.kind == "tool" and event.call is not None:
                    args = event.call.args if isinstance(event.call.args, dict) else {}
                    tool_calls.append({
                        "turn": event.turn,
                        "name": event.call.name,
                        "args": _safe_args(args),
                        "ok": bool(event.outcome.ok) if event.outcome is not None else False,
                        "status": event.outcome.presentation_status() if event.outcome is not None else "",
                    })
            if runner.result is None:
                raise RuntimeError("research finished without result")
            iteration = ResearchIterationRun(result=runner.result, tools=runner.tools)
            iterations.append(iteration)
            return iteration

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
                actions = _safe_model_actions(run_provider.last_turn, run_provider.last_reply)[:3]
                model_actions.extend(actions)
                for action in actions or [{"tool": "", "args": {}}]:
                    tool_calls.append({
                        "turn": run_provider.last_turn,
                        "name": str(action.get("tool") or ""),
                        "args": action.get("args", {}) if isinstance(action.get("args"), dict) else {},
                        "ok": bool(result.ok),
                        "status": result.stop_reason or ("ok" if result.ok else "error"),
                    })
            return result

        try:
            context = ResearchContext(
                question=case.question,
                session_id=f"bounded-planner-ab-{provider_id}-{case.name}-{arm}",
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
            materials: list[PlanExecutionResult] = []
            pipeline_result = _run_pipeline_with_ab_experiment(
                pipeline,
                arm=arm,
                materials=materials,
            )
            result = pipeline_result.final_result
            proof = (
                review_research_proof(result.research_record, question=case.question)
                if result.research_record is not None
                else None
            )
            record_counts = _record_counts(result.research_record)
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
                "record_source_count": record_counts["source_count"],
                "record_evidence_count": record_counts["evidence_count"],
                "record_claim_count": record_counts["claim_count"],
                "unsupported_claim_count": record_counts["unsupported_claim_count"],
                "unsupported_claim_rate": _ratio(
                    record_counts["unsupported_claim_count"],
                    record_counts["claim_count"],
                ),
                "proof_ok": bool(proof.ok) if proof is not None else False,
                "proof_answer_status": proof.answer_status if proof is not None else "",
                "proof_coverage": proof.answer_coverage_score if proof is not None else None,
                "citation_locator_verified": (
                    bool(proof.citation_locator_verified) if proof is not None else False
                ),
                "support_relation_verified": (
                    bool(proof.support_relation_verified) if proof is not None else False
                ),
                "counterevidence_checked": (
                    bool(proof.counterevidence_checked) if proof is not None else False
                ),
                "proof_missing_evidence": list(proof.missing_evidence[:8]) if proof is not None else [],
                "expected_terms_present": _expected_terms_present(result.summary, case.expected_terms),
                "followup_applied": pipeline_result.followup_applied,
                "followup_rounds": pipeline_result.followup_rounds,
                "pipeline_stop_reason": pipeline_result.stop_reason,
                "planner_stop_reason": pipeline_result.planner_stop_reason,
                "fresh_source_count": pipeline_result.fresh_source_count,
                "new_evidence_count": pipeline_result.new_evidence_count,
                "final_evidence_count": pipeline_result.final_evidence_count,
                "attempted_fresh_source_count": pipeline_result.attempted_fresh_source_count,
                "attempted_new_evidence_count": pipeline_result.attempted_new_evidence_count,
                "ab_followup_mode": (
                    AB_EVIDENCE_ONLY_FOLLOWUP_MODE
                    if arm == "planner" and pipeline_result.followup_rounds
                    else ""
                ),
                "ab_followup_max_turns": (
                    1 if arm == "planner" and pipeline_result.followup_rounds else 0
                ),
                "fixture_queries": search.queries[:12],
                "fixture_fetches": search.fetches[:12],
                "provider_send_count": run_provider.send_index,
                "provider_reply_count": run_provider.reply_count,
                "provider_prompt_chars": run_provider.prompt_chars,
                "provider_reply_chars": run_provider.reply_chars,
                "model_actions": model_actions[:60],
                "tool_calls": tool_calls[:60],
                "info": infos[:16],
                "summary_chars": len(result.summary or ""),
                "summary_preview": _clip(result.summary, 1600),
            }
            row["score"] = _score_row(row)
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
                "fixture_queries": search.queries[:12],
                "fixture_fetches": search.fetches[:12],
                "provider_send_count": run_provider.send_index,
                "provider_reply_count": run_provider.reply_count,
                "provider_prompt_chars": run_provider.prompt_chars,
                "provider_reply_chars": run_provider.reply_chars,
                "model_actions": model_actions[:60],
                "tool_calls": tool_calls[:60],
                "info": infos[:16],
            }
            if trace is not None:
                trace.record_case_complete(case=case.name, arm=arm, row=row)
            return row
        finally:
            store.close()


def _config_for_arm(arm: str) -> ResearchPipelineConfig:
    if arm == "baseline":
        return ResearchPipelineConfig(enabled=False, max_followup_rounds=0)
    return ResearchPipelineConfig(
        enabled=True,
        max_followup_rounds=1,
        max_queries_per_round=3,
        max_sources_per_query=2,
        max_total_sources=6,
    )


def _run_pipeline_with_ab_experiment(
    pipeline: ResearchPipeline,
    *,
    arm: str,
    materials: list[PlanExecutionResult] | None = None,
):
    if arm != "planner":
        return pipeline.run()
    from codey.research import pipeline as pipeline_module

    original_executor = pipeline_module.PlanExecutor
    original_has_actionable_gap = pipeline_module._has_actionable_gap
    original_pipeline_stop_reason = pipeline_module._pipeline_stop_reason
    original_is_followup_eligible_stop = pipeline_module._is_followup_eligible_stop

    class _FreshMaterialExecutor(FreshMaterialPlanExecutor):
        def __init__(self, *, config=None, should_stop=None) -> None:
            super().__init__(
                config=config or ResearchPipelineConfig(),
                should_stop=should_stop or (lambda: False),
            )

        def execute(self, plan: ResearchPlan, tools: ResearchTools) -> PlanExecutionResult:
            material = super().execute(plan, tools)
            if materials is not None:
                materials.append(material)
            return material

    def ab_pipeline_stop_reason(plan, review=None):
        if _ab_has_actionable_gap(review) and not _ab_needs_new_material(review):
            return "no_new_material_needed"
        return original_pipeline_stop_reason(plan, review)

    try:
        pipeline_module.PlanExecutor = _FreshMaterialExecutor
        pipeline_module._has_actionable_gap = _ab_needs_new_material
        pipeline_module._pipeline_stop_reason = ab_pipeline_stop_reason
        pipeline_module._is_followup_eligible_stop = lambda stop_reason: str(stop_reason or "") in {
            "done",
            "max_turns",
            "no_progress",
            "protocol",
        }
        return pipeline.run()
    finally:
        pipeline_module.PlanExecutor = original_executor
        pipeline_module._has_actionable_gap = original_has_actionable_gap
        pipeline_module._pipeline_stop_reason = original_pipeline_stop_reason
        pipeline_module._is_followup_eligible_stop = original_is_followup_eligible_stop


def _ab_has_actionable_gap(review: object | None) -> bool:
    if review is None or bool(getattr(review, "ok", False)):
        return False
    if _ab_needs_new_material(review):
        return True
    missing = set(getattr(review, "missing_evidence", ()) or ())
    return bool(
        missing.intersection({
            "assumption_used_as_answer",
            "claim_missing_citation",
            "claim_missing_evidence_ref",
            "claim_missing_support_relation",
            "claim_not_evidence_backed",
            "support_relation_bad_locator",
            "support_relation_missing_evidence",
            "unsupported_claims",
        })
        or getattr(review, "overclaim_warnings", ())
    )


def _ab_needs_new_material(review: object | None) -> bool:
    if review is None or bool(getattr(review, "ok", False)):
        return False
    answer_status = str(getattr(review, "answer_status", "") or "")
    if answer_status in {"not_answered", "insufficient_evidence"}:
        return True
    missing = set(getattr(review, "missing_evidence", ()) or ())
    return bool(
        getattr(review, "coverage_gaps", ())
        or getattr(review, "followup_questions", ())
        or getattr(review, "query_rewrite_candidates", ())
        or missing.intersection({
            "answer_coverage_gap",
            "counterevidence_not_checked",
            "partial_answer",
        })
        or getattr(review, "source_trust_warnings", ())
    )


def _safe_args(args: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in args.items():
        if key in {"query", "url", "source_id", "result_id", "hit_id", "pages", "type", "title"}:
            safe[str(key)] = _clip(value, 240)
        elif key in {"offset", "limit"}:
            safe[str(key)] = value
    return safe


def _safe_model_actions(turn: int, reply: str) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for obj in extract_json_objects(reply or ""):
        tool = str(obj.get("tool") or "").strip().lower()
        args = obj.get("args")
        if not isinstance(args, dict):
            args = {}
        actions.append({
            "turn": int(turn or 0),
            "tool": _clip(tool, 80),
            "args": _safe_args(args),
        })
    if not actions and str(reply or "").strip():
        actions.append({"turn": int(turn or 0), "tool": "<no_json>", "args": {}})
    return actions


def _expected_terms_present(summary: str, expected_terms: tuple[str, ...]) -> bool:
    text = str(summary or "").casefold()
    return all(term.casefold() in text for term in expected_terms)


def _record_counts(record: object) -> dict[str, int]:
    payload: dict[str, object] = {}
    to_jsonable = getattr(record, "to_jsonable", None)
    if callable(to_jsonable):
        try:
            raw = to_jsonable()
            if isinstance(raw, dict):
                payload = raw
        except Exception:
            payload = {}
    elif isinstance(record, dict):
        payload = record
    return {
        "source_count": _count_from_payload(payload, "source_count", "sources"),
        "evidence_count": _count_from_payload(payload, "evidence_count", "evidence"),
        "claim_count": _count_from_payload(payload, "claim_count", "claims"),
        "unsupported_claim_count": _count_from_payload(
            payload,
            "unsupported_claim_count",
            "unsupported_claims",
        ),
    }


def _count_from_payload(payload: dict[str, object], count_key: str, list_key: str) -> int:
    raw_count = payload.get(count_key)
    if not isinstance(raw_count, bool):
        try:
            return max(0, int(raw_count))
        except (TypeError, ValueError):
            pass
    raw_list = payload.get(list_key)
    if isinstance(raw_list, (list, tuple)):
        return len(raw_list)
    return 0


def _ratio(numerator: object, denominator: object) -> float:
    den = _float(denominator)
    if den <= 0:
        return 0.0
    return round(max(0.0, _float(numerator)) / den, 3)


def _float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _score_row(row: dict[str, Any]) -> int:
    status = str(row.get("proof_answer_status") or "")
    status_score = {
        "answered": 4,
        "partial": 2,
        "insufficient_evidence": 1,
        "not_answered": 0,
    }.get(status, 0)
    return (
        (4 if row.get("proof_ok") else 0)
        + status_score
        + min(3, int(row.get("evidence_count") or 0))
        + (2 if row.get("expected_terms_present") else 0)
    )


def _answer_status_rank(status: object) -> int:
    return {
        "answered": 3,
        "partial": 2,
        "insufficient_evidence": 1,
        "not_answered": 0,
    }.get(str(status or ""), 0)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"rows": len(rows), "by_case": {}}
    for case in sorted({str(row.get("case") or "") for row in rows if row.get("case")}):
        case_rows = [row for row in rows if row.get("case") == case]
        arms = {str(row.get("arm") or ""): row for row in case_rows}
        baseline = arms.get("baseline", {})
        planner = arms.get("planner", {})
        usefulness = _followup_usefulness(baseline, planner)
        summary["by_case"][case] = {
            "baseline_score": baseline.get("score"),
            "planner_score": planner.get("score"),
            "delta": (
                int(planner.get("score") or 0) - int(baseline.get("score") or 0)
                if baseline and planner
                else None
            ),
            "planner_followup_rounds": planner.get("followup_rounds"),
            "planner_stop_reason": planner.get("planner_stop_reason"),
            "baseline_answer_status": baseline.get("proof_answer_status"),
            "planner_answer_status": planner.get("proof_answer_status"),
            "followup_usefulness": usefulness,
        }
    return summary


def _followup_usefulness(
    baseline: dict[str, Any],
    planner: dict[str, Any],
) -> dict[str, Any]:
    if not baseline or not planner:
        return {"evaluated": False}
    baseline_ok = bool(baseline.get("ok"))
    planner_ok = bool(planner.get("ok"))
    if not baseline_ok or not planner_ok:
        return {
            "evaluated": False,
            "reason": "row_not_ok",
            "baseline_ok": baseline_ok,
            "planner_ok": planner_ok,
        }
    coverage_delta = round(
        _float(planner.get("proof_coverage")) - _float(baseline.get("proof_coverage")),
        3,
    )
    unsupported_rate_delta = round(
        _float(planner.get("unsupported_claim_rate")) - _float(baseline.get("unsupported_claim_rate")),
        3,
    )
    score_delta = int(planner.get("score") or 0) - int(baseline.get("score") or 0)
    source_delta = int(planner.get("record_source_count") or 0) - int(baseline.get("record_source_count") or 0)
    evidence_delta = int(planner.get("record_evidence_count") or 0) - int(baseline.get("record_evidence_count") or 0)
    query_delta = len(planner.get("fixture_queries") or ()) - len(baseline.get("fixture_queries") or ())
    fetch_delta = len(planner.get("fixture_fetches") or ()) - len(baseline.get("fixture_fetches") or ())
    baseline_fetch_urls = {str(item or "") for item in baseline.get("fixture_fetches") or ()}
    planner_fetch_urls = {str(item or "") for item in planner.get("fixture_fetches") or ()}
    new_fetched_urls = tuple(sorted(url for url in planner_fetch_urls - baseline_fetch_urls if url))
    send_delta = int(planner.get("provider_send_count") or 0) - int(baseline.get("provider_send_count") or 0)
    seconds_delta = round(_float(planner.get("seconds")) - _float(baseline.get("seconds")), 3)
    answer_status_delta = _answer_status_rank(planner.get("proof_answer_status")) - _answer_status_rank(
        baseline.get("proof_answer_status")
    )
    reasons: list[str] = []
    quality_reasons: list[str] = []
    quality_regressions: list[str] = []
    if coverage_delta >= 0.05:
        reasons.append("coverage_improved")
        quality_reasons.append("coverage_improved")
    elif coverage_delta <= -0.05:
        quality_regressions.append("coverage_regressed")
    if unsupported_rate_delta <= -0.02:
        reasons.append("unsupported_rate_improved")
        quality_reasons.append("unsupported_rate_improved")
    elif unsupported_rate_delta >= 0.02:
        quality_regressions.append("unsupported_rate_regressed")
    if evidence_delta > 0:
        reasons.append("new_evidence")
    if source_delta > 0:
        reasons.append("new_sources")
    if new_fetched_urls:
        reasons.append("new_fetched_sources")
    if answer_status_delta > 0:
        reasons.append("answer_status_improved")
        quality_reasons.append("answer_status_improved")
    elif answer_status_delta < 0:
        quality_regressions.append("answer_status_regressed")
    if planner.get("proof_ok") and not baseline.get("proof_ok"):
        reasons.append("proof_ok_recovered")
        quality_reasons.append("proof_ok_recovered")
    elif baseline.get("proof_ok") and not planner.get("proof_ok"):
        quality_regressions.append("proof_ok_regressed")
    if score_delta > 0:
        reasons.append("score_improved")
    elif score_delta < 0:
        quality_regressions.append("score_regressed")
    if planner.get("expected_terms_present") and not baseline.get("expected_terms_present"):
        reasons.append("expected_terms_recovered")
        quality_reasons.append("expected_terms_recovered")
    elif baseline.get("expected_terms_present") and not planner.get("expected_terms_present"):
        quality_regressions.append("expected_terms_lost")
    material_gain = bool(source_delta > 0 or evidence_delta > 0)
    execution_material_gain = bool(new_fetched_urls)
    quality_gain = bool(quality_reasons)
    quality_regression = bool(quality_regressions)
    useful = bool(
        int(planner.get("followup_rounds") or 0) > 0
        and material_gain
        and quality_gain
        and not quality_regression
    )
    return {
        "evaluated": True,
        "useful": useful,
        "material_gain": material_gain,
        "execution_material_gain": execution_material_gain,
        "quality_gain": quality_gain,
        "quality_regression": quality_regression,
        "reason_codes": reasons,
        "quality_regression_codes": quality_regressions,
        "followup_rounds": int(planner.get("followup_rounds") or 0),
        "new_sources": max(0, source_delta),
        "new_evidence": max(0, evidence_delta),
        "new_fetched_sources": len(new_fetched_urls),
        "new_fetched_source_urls": [_clip(url, 160) for url in new_fetched_urls[:6]],
        "answer_coverage_delta": coverage_delta,
        "unsupported_claim_rate_delta": unsupported_rate_delta,
        "answer_status_delta": answer_status_delta,
        "score_delta": score_delta,
        "query_delta": query_delta,
        "fetch_delta": fetch_delta,
        "provider_send_delta": send_delta,
        "seconds_delta": seconds_delta,
    }


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
    run_id: str,
) -> dict[str, Any]:
    payload = load_or_new_payload(
        output,
        probe="bounded_research_planner_ab",
        provider_id=provider_id,
        cases=cases,
        arms=arms,
    )
    normalize_payload_metadata(payload, provider_id=provider_id, cases=cases, arms=arms)
    if trace is not None:
        payload["trace_output"] = str(trace.directory)
    existing = {
        (str(row.get("case") or ""), str(row.get("arm") or ""))
        for row in payload["rows"]
        if row.get("ok") or not rerun_failed
    }
    pending = _pending_case_keys(cases=cases, arms=arms, existing=existing)
    if trace is not None:
        trace.record_run_start(
            cases=tuple(case.name for case in cases),
            arms=arms,
            max_turns=max_turns,
        )
    if not pending:
        if trace is not None:
            trace.append_event(
                event_type="note",
                stage="no_pending_rows",
                facts={
                    "output": str(output)[:200],
                    "cases": [case.name for case in cases],
                    "arms": list(arms),
                    "rerun_failed": bool(rerun_failed),
                    "existing_rows": len(payload["rows"]),
                },
            )
            trace.record_run_complete(rows=len(payload["rows"]))
        payload["complete"] = True
        payload["summary"] = summarize(payload["rows"])
        payload["updated_at"] = timestamp()
        write_payload_bounded(output, payload)
        print(
            f"[{provider_id}] no pending rows for cases={','.join(case.name for case in cases)} "
            f"arms={','.join(arms)}; use --rerun-failed or a new --output to run again.",
            flush=True,
        )
        return payload
    write_payload_bounded(output, payload)
    provider_controls.begin_task_context(f"bounded-research-planner-ab:{provider_id}")
    provider = None
    try:
        with fixture_url_policy_bypass():
            provider = TimedProvider(
                connect_provider(
                    provider_id,
                    port=port,
                    open_if_missing=open_if_missing,
                    bring_to_front=open_if_missing,
                ),
                send_timeout=send_timeout,
                new_chat_timeout=new_chat_timeout,
            )
            for case in cases:
                for arm in arms:
                    key = (case.name, arm)
                    if key in existing:
                        continue
                    row = run_case(
                        provider,
                        provider_id=provider_id,
                        case=case,
                        arm=arm,
                        max_turns=max_turns,
                        run_id=run_id,
                        trace=trace,
                    )
                    payload["rows"].append(row)
                    payload["summary"] = summarize(payload["rows"])
                    payload["updated_at"] = timestamp()
                    write_payload_bounded(output, payload)
                    print(
                        f"[{provider_id} {case.name} {arm}] "
                        f"ok={row.get('ok')} score={row.get('score')} "
                        f"followup={row.get('followup_rounds')} "
                        f"stop={row.get('planner_stop_reason') or row.get('stop_reason', row.get('error', ''))}",
                        flush=True,
                    )
            payload["complete"] = True
            payload["summary"] = summarize(payload["rows"])
            payload["updated_at"] = timestamp()
            write_payload_bounded(output, payload)
            if trace is not None:
                trace.record_run_complete(rows=len(payload["rows"]))
            return payload
    finally:
        provider_controls.end_task_context()
        if provider is not None:
            try:
                provider.close()
            except Exception:
                pass


def _pending_case_keys(
    *,
    cases: tuple[Case, ...],
    arms: tuple[str, ...],
    existing: set[tuple[str, str]],
) -> list[tuple[str, str]]:
    return [
        (case.name, arm)
        for case in cases
        for arm in arms
        if (case.name, arm) not in existing
    ]


def _default_output(provider_id: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return RESULTS_DIR / f"bounded_research_planner_ab-{provider_id}-{stamp}.json"


def _clip(value: object, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if limit <= 3:
        return text[:limit]
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _parse_names(raw_values: list[str] | None, allowed: Any, label: str) -> tuple[str, ...]:
    if not raw_values:
        return tuple(allowed)
    names: list[str] = []
    for raw in raw_values:
        for part in str(raw or "").split(","):
            name = part.strip()
            if name:
                names.append(name)
    unknown = [name for name in names if name not in allowed]
    if unknown:
        raise SystemExit(f"unknown {label}: {', '.join(unknown)}")
    return tuple(dict.fromkeys(names))


def _self_test() -> None:
    rows = [
        {
            "case": "warehouse_gap",
            "arm": "baseline",
            "ok": True,
            "score": 3,
            "proof_answer_status": "partial",
            "proof_coverage": 0.62,
            "unsupported_claim_rate": 0.14,
            "record_source_count": 1,
            "record_evidence_count": 1,
            "fixture_queries": ["benefit"],
            "fixture_fetches": ["https://www.nrel.gov/docs/lithium-storage-benefit"],
            "provider_send_count": 4,
            "seconds": 12.0,
        },
        {
            "case": "warehouse_gap",
            "arm": "planner",
            "ok": True,
            "score": 8,
            "proof_answer_status": "answered",
            "proof_coverage": 0.91,
            "unsupported_claim_rate": 0.08,
            "record_source_count": 2,
            "record_evidence_count": 3,
            "fixture_queries": ["benefit", "limitation"],
            "fixture_fetches": [
                "https://www.nrel.gov/docs/lithium-storage-benefit",
                "https://www.nrel.gov/docs/lithium-storage-limit",
            ],
            "provider_send_count": 6,
            "seconds": 20.0,
            "followup_rounds": 1,
            "planner_stop_reason": "max_followup_rounds",
        },
    ]
    summary = summarize(rows)
    assert summary["by_case"]["warehouse_gap"]["delta"] == 5
    usefulness = summary["by_case"]["warehouse_gap"]["followup_usefulness"]
    assert usefulness["useful"] is True
    assert usefulness["material_gain"] is True
    assert usefulness["execution_material_gain"] is True
    assert usefulness["quality_gain"] is True
    assert usefulness["quality_regression"] is False
    assert usefulness["new_sources"] == 1
    assert usefulness["new_evidence"] == 2
    assert usefulness["new_fetched_sources"] == 1
    assert usefulness["answer_coverage_delta"] == 0.29
    assert usefulness["unsupported_claim_rate_delta"] == -0.06
    assert usefulness["provider_send_delta"] == 2
    assert _config_for_arm("baseline").enabled is False
    assert _config_for_arm("planner").enabled is True
    fixture = FixtureSearchProvider(CASES["widget_noop"].documents)
    assert [item["url"] for item in fixture.search("current primary source evidence")] == [
        "https://source-a.test/widget-storage"
    ]
    with fixture_material_phase(fixture):
        assert [item["url"] for item in fixture.search("current primary source evidence")] == [
            "https://source-b.test/widget-storage-update",
            "https://source-a.test/widget-storage",
        ]
    material = PlanExecutionResult(
        queries_executed=("current primary source evidence",),
        opened_sources=(
            {
                "final_url": "https://source-b.test/widget-storage-update",
                "title": "Benchmark source B",
            },
        ),
        previews=(
            "query: current primary source evidence\n"
            "Benchmark source B | https://source-b.test/widget-storage-update\n"
            "The Widget Storage working group has not adopted a stable-v3 successor; stable-v2 remains the recommended endpoint.",
        ),
        stop_reason="opened_sources",
    )
    executor = FreshMaterialPlanExecutor(
        config=ResearchPipelineConfig(max_queries_per_round=1, max_sources_per_query=2, max_total_sources=2),
        should_stop=lambda: False,
    )
    with tempfile.TemporaryDirectory(prefix="codey-bounded-planner-ab-self-") as td:
        from codey.knowledge.changes import KnowledgeChanges
        from codey.research.source_document import SourceDocument

        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        try:
            tools = ResearchTools(
                search=fixture,
                store=store,
                changes=KnowledgeChanges(root=store.root),
                session_id="self-test",
            )
            tools.sources_read.add("https://source-a.test/widget-storage")
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url="https://source-a.test/widget-storage",
                final_url="https://source-a.test/widget-storage",
                title="Benchmark source A",
                text=(
                    "The Widget Storage standard still recommends the stable-v2 "
                    "endpoint for client storage integration."
                ),
            ))
            with fixture_url_policy_bypass():
                material_result = executor.execute(
                    ResearchPlan(
                        plan_ref="research_plan:" + "d" * 16,
                        query_candidates=(
                            QueryCandidate("research_query:" + "d" * 16, "current primary source evidence"),
                        ),
                        max_queries=1,
                        max_sources=2,
                    ),
                    tools,
                )
            assert material_result.fresh_source_urls == ("https://source-b.test/widget-storage-update",)
            assert material_result.fresh_source_count == 1
        finally:
            store.close()
    with tempfile.TemporaryDirectory(prefix="codey-bounded-planner-ab-self-") as td:
        from codey.knowledge.changes import KnowledgeChanges
        from codey.research.source_document import SourceDocument

        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        try:
            tools = ResearchTools(
                search=None,
                store=store,
                changes=KnowledgeChanges(root=store.root),
                session_id="self-test",
            )
            url = "https://source-b.test/widget-storage-update"
            tools.sources_read.add(url)
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url=url,
                final_url=url,
                title="Benchmark source B",
                text=(
                    "The Widget Storage working group has not adopted a stable-v3 "
                    "successor; stable-v2 remains the recommended endpoint."
                ),
            ))

            class _ReplyProvider:
                def send(self, prompt: str) -> str:
                    assert '"tool": "knowledge_write"' in prompt
                    return json.dumps({
                        "tool": "knowledge_write",
                        "args": {
                            "type": "fact",
                            "title": "Evidence from Benchmark source B",
                            "body": (
                                "The Widget Storage working group has not adopted a stable-v3 "
                                "successor; stable-v2 remains the recommended endpoint."
                            ),
                            "sources": [url],
                            "evidence": [{
                                "claim": "stable-v2 remains the recommended endpoint.",
                                "source_url": url,
                                "excerpt": (
                                    "The Widget Storage working group has not adopted a stable-v3 "
                                    "successor; stable-v2 remains the recommended endpoint."
                                ),
                                "stance": "supports",
                            }],
                        },
                    })

            replay = run_evidence_followup(
                provider=_ReplyProvider(),
                tools=tools,
                plan=ResearchPlan(plan_ref="research_plan:" + "e" * 16),
                material=PlanExecutionResult(
                    fresh_source_urls=(url,),
                    opened_sources=({"final_url": url, "title": "Benchmark source B"},),
                    previews=material.previews,
                    stop_reason="opened_sources",
                ),
                question=CASES["widget_noop"].question,
            )
            assert replay.ok is True
            assert replay.stop_reason == "written"
            assert replay.new_evidence_count == 1
        finally:
            store.close()
    failed_usefulness = _followup_usefulness(
        {"arm": "baseline", "ok": False, "error": "timeout"},
        {
            "arm": "planner",
            "ok": True,
            "followup_rounds": 1,
            "score": 8,
            "proof_coverage": 0.9,
            "record_source_count": 2,
            "record_evidence_count": 2,
        },
    )
    assert failed_usefulness["evaluated"] is False
    assert failed_usefulness["reason"] == "row_not_ok"
    material_only = _followup_usefulness(
        {
            "arm": "baseline",
            "ok": True,
            "score": 8,
            "proof_answer_status": "answered",
            "proof_coverage": 0.9,
            "unsupported_claim_rate": 0.0,
            "record_source_count": 1,
            "record_evidence_count": 1,
        },
        {
            "arm": "planner",
            "ok": True,
            "score": 7,
            "proof_answer_status": "partial",
            "proof_coverage": 0.8,
            "unsupported_claim_rate": 0.1,
            "record_source_count": 2,
            "record_evidence_count": 2,
            "followup_rounds": 1,
        },
    )
    assert material_only["material_gain"] is True
    assert material_only["quality_regression"] is True
    assert material_only["useful"] is False
    assert _expected_terms_present("stable-v2 endpoint", ("stable-v2",))
    assert journal_directory_for_output(Path("tests/manual/results/bounded_research_planner_ab-deepseek.json")).name == (
        "bounded_research_planner_ab-deepseek.trace"
    )
    assert _pending_case_keys(
        cases=(CASES["warehouse_gap"],),
        arms=("baseline",),
        existing=set(),
    ) == [("warehouse_gap", "baseline")]
    with tempfile.TemporaryDirectory(prefix="codey-bounded-planner-ab-self-") as td:
        trace_dir = journal_directory_for_output(Path(td) / "trace.json")
        trace = ABJournalWriter(
            directory=trace_dir,
            experiment_id="bounded_research_planner_ab",
            run_id="run-self",
            provider="deepseek",
        )
        trace.record_send_start(
            case="warehouse_gap",
            arm="baseline",
            turn=1,
            prompt="prompt text",
        )
        trace.record_reply(
            case="warehouse_gap",
            arm="baseline",
            turn=1,
            prompt="prompt text",
            reply='{"tool":"done","args":{"answer":"ok"}}',
        )
        trace.close()
        reader = ABJournalReader(trace_dir)
        assert [event["event_type"] for event in reader.events()] == ["send_start", "reply"]
        manifest = json.loads((trace_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["experiment_id"] == "bounded_research_planner_ab"
        appended_trace = ABJournalWriter(
            directory=trace_dir,
            experiment_id="bounded_research_planner_ab",
            run_id="run-self",
            provider="deepseek",
        )
        appended_trace.record_case_complete(
            case="warehouse_gap",
            arm="planner",
            row={"ok": True, "score": 1, "stop_reason": "done", "summary_preview": "ok"},
        )
        appended_trace.close()
        reader = ABJournalReader(trace_dir)
        events = reader.events()
        assert [event["event_type"] for event in events] == [
            "send_start",
            "reply",
            "case_complete",
        ]
        assert all(event["previous_digest"].startswith("sha256:") for event in events)
        output = Path(td) / "payload.json"
        output.write_text(
            json.dumps({"probe": "bounded_research_planner_ab", "provider": "qwen", "rows": []}),
            encoding="utf-8",
        )
        try:
            load_or_new_payload(
                output,
                probe="bounded_research_planner_ab",
                provider_id="deepseek",
                cases=(CASES["warehouse_gap"],),
                arms=("baseline",),
            )
        except OutputProviderMismatch as exc:
            assert exc.expected == "deepseek"
            assert exc.found == "qwen"
        else:
            raise AssertionError("provider mismatch was not rejected")
        payload = {
            "probe": "bounded_research_planner_ab",
            "provider": "deepseek",
            "rows": [],
            "cases": ["warehouse_gap"],
            "arms": ["baseline"],
        }
        normalize_payload_metadata(
            payload,
            provider_id="deepseek",
            cases=(CASES["warehouse_gap"], CASES["widget_noop"]),
            arms=("planner",),
        )
        assert payload["cases"] == ["warehouse_gap", "widget_noop"]
        assert payload["arms"] == ["baseline", "planner"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Live A/B for 0.4.4 bounded Research planner")
    parser.add_argument("--provider", choices=(*WEB_PROVIDERS, "all"), default="deepseek")
    parser.add_argument("--case", action="append", help="case name or comma list; defaults to all cases")
    parser.add_argument("--arms", default="baseline,planner")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--trace-output", type=Path, default=None, help="journal directory; default is <output-stem>.trace/ next to the result file")
    parser.add_argument("--max-turns", type=int, default=14)
    parser.add_argument("--send-timeout", type=float, default=120)
    parser.add_argument("--new-chat-timeout", type=float, default=60)
    parser.add_argument("--open-if-missing", action="store_true")
    parser.add_argument("--rerun-failed", action="store_true")
    parser.add_argument("--no-live-trace", action="store_true", help="disable the durable JSONL observation journal")
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
        # run_id follows the FINAL provider-specific output name so that
        # resuming `custom-deepseek.json` individually reuses the exact
        # journal identity the all-mode run created.
        run_id = output.stem
        trace: ABJournalWriter | None = None
        if not args.no_live_trace:
            if args.trace_output is not None:
                trace_output = args.trace_output
                if args.provider == "all":
                    trace_output = args.trace_output.with_name(
                        f"{args.trace_output.stem}-{provider_id}{args.trace_output.suffix}"
                    )
            else:
                trace_output = journal_directory_for_output(output)
            try:
                trace = ABJournalWriter(
                    directory=trace_output,
                    experiment_id="bounded_research_planner_ab",
                    run_id=run_id,
                    provider=provider_id,
                )
            except ABJournalIdentityMismatch as exc:
                print(str(exc), file=sys.stderr)
                return 2
        try:
            run_provider(
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
                run_id=run_id,
            )
        except OutputProviderMismatch as exc:
            print(str(exc), file=sys.stderr)
            return 2
        finally:
            if trace is not None:
                trace.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())