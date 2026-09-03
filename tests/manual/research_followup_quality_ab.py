"""Live connector-backed A/B for Research follow-up quality.

Both arms use the production connector-aware search path. The baseline disables
bounded follow-up; the planner arm enables one production evidence-only
follow-up round and deterministic merge. This isolates whether real connector
material plus follow-up improves the final ResearchRecord.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.knowledge.store import KnowledgeStore
from codey.providers import controls as provider_controls
from codey.providers.registry import connect_provider
from codey.research.context import ResearchContext, ResearchPipelineConfig
from codey.research.evidence_followup import run_evidence_followup
from codey.research.evidence_ledger import EvidenceLedgerStore
from codey.research.followup_quality import followup_usefulness, score_followup_quality_row
from codey.research.pipeline import ResearchIterationRun, ResearchPipeline
from codey.research.proof_quality import review_research_proof
from codey.research.runner import ResearchRunner
from tests.manual import bounded_research_planner_ab as bounded
from tests.manual import source_connector_ab as connector
from tests.manual.ab_harness_common import (
    AB_FAILURE_CODEY,
    AB_FAILURE_NONE,
    ABJournalWriter,
    ArmRunLayout,
    ResultRowStore,
    TracingProvider,
    bind_row_evidence_refs,
    row_has_terminal_failure,
    timestamp,
    write_arm_manifest,
    build_arm_manifest,
)
from tests.manual.ab_journal import (
    ABJournalIdentityMismatch,
    TranscriptReplayCache,
    TRANSCRIPT_MODE_DIGEST_ONLY,
)

PROBE = "research_followup_quality_ab"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
ARMS = ("baseline", "planner")
DEFAULT_ARMS = "baseline,planner"
WEB_PROVIDERS = connector.WEB_PROVIDERS
CASES = {name: case for name, case in connector.CASES.items() if name in {"pubmed", "arxiv"}}


def config_for_arm(arm: str) -> ResearchPipelineConfig:
    if arm == "baseline":
        return ResearchPipelineConfig(enabled=False, max_followup_rounds=0)
    return ResearchPipelineConfig(
        enabled=True,
        max_followup_rounds=1,
        max_queries_per_round=3,
        max_sources_per_query=2,
        max_total_sources=6,
        max_source_preview_chars=2400,
        max_followup_context_chars=8000,
    )


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
            "planner_followup_rounds": planner.get("followup_rounds"),
            "planner_stop_reason": planner.get("planner_stop_reason"),
            "followup_usefulness": usefulness,
        }
    return summary


def gate_ok(rows: list[dict[str, Any]], complete: bool) -> bool:
    if not complete:
        return False
    if any(row_has_terminal_failure(row) for row in rows):
        return False
    by_case = summarize(rows).get("by_case")
    if not isinstance(by_case, Mapping) or not by_case:
        return False
    return all(isinstance(item, Mapping) and item.get("delta") is not None for item in by_case.values())


def run_case(
    provider,
    *,
    provider_id: str,
    case: connector.Case,
    arm: str,
    max_turns: int,
    run_id: str,
    trace: ABJournalWriter | None,
    layout: ArmRunLayout,
) -> dict[str, Any]:
    started = time.time()
    tool_calls: list[dict[str, Any]] = []
    model_actions: list[dict[str, Any]] = []
    infos: list[str] = []
    with tempfile.TemporaryDirectory(prefix="codey-research-followup-quality-ab-") as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        evidence_ledgers = EvidenceLedgerStore(root / "state")
        search = connector._search_provider_for_arm("connector")
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
            topic_continuity_context: str = "",
            topic_continuity_payload: Mapping[str, object] | None = None,
        ) -> ResearchIterationRun:
            runner = ResearchRunner(
                run_provider,
                search,
                store,
                max_turns=max_turns,
                session_id=f"research-followup-quality-ab-{provider_id}-{case.name}-{arm}",
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
                    model_actions.extend(connector._safe_model_actions(event.turn, event.reply)[:3])
                if event.kind == "info":
                    infos.append(str(event.message or "")[:240])
                if event.kind == "tool" and event.call is not None:
                    args = event.call.args if isinstance(event.call.args, dict) else {}
                    tool_calls.append(
                        {
                            "turn": event.turn,
                            "name": event.call.name,
                            "args": connector._safe_args(args),
                            "ok": bool(event.outcome.ok) if event.outcome is not None else False,
                            "status": event.outcome.presentation_status() if event.outcome is not None else "",
                        }
                    )
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
                actions = connector._safe_model_actions(run_provider.last_turn, run_provider.last_reply)[:3]
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
                session_id=f"research-followup-quality-ab-{provider_id}-{case.name}-{arm}",
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
                config=config_for_arm(arm),
            )
            pipeline_result = pipeline.run()
            result = pipeline_result.final_result
            proof = (
                review_research_proof(result.research_record, question=case.question)
                if result.research_record is not None
                else None
            )
            record_counts = bounded._record_counts(result.research_record)
            connector_errors = list(getattr(search, "last_connector_errors", []))[:8]
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
                "opened_target_host": connector._opened_target_host(result.source_urls, case.target_hosts),
                "evidence_count": len(result.evidence_items),
                "record_source_count": record_counts["source_count"],
                "record_evidence_count": record_counts["evidence_count"],
                "record_claim_count": record_counts["claim_count"],
                "unsupported_claim_count": record_counts["unsupported_claim_count"],
                "unsupported_claim_rate": bounded._ratio(
                    record_counts["unsupported_claim_count"],
                    record_counts["claim_count"],
                ),
                "proof_ok": bool(proof.ok) if proof is not None else False,
                "proof_answer_status": proof.answer_status if proof is not None else "",
                "proof_coverage": proof.answer_coverage_score if proof is not None else None,
                "citation_locator_verified": bool(proof.citation_locator_verified) if proof is not None else False,
                "support_relation_verified": bool(proof.support_relation_verified) if proof is not None else False,
                "counterevidence_checked": bool(proof.counterevidence_checked) if proof is not None else False,
                "proof_missing_evidence": list(proof.missing_evidence[:8]) if proof is not None else [],
                "expected_terms_present": connector._expected_terms_present(result.summary, case.expected_terms),
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
                    "connector_backed_evidence_followup"
                    if arm == "planner" and pipeline_result.followup_rounds
                    else ""
                ),
                "connector_errors": connector_errors,
                "provider_send_count": run_provider.send_index,
                "provider_reply_count": run_provider.reply_count,
                "provider_prompt_chars": run_provider.prompt_chars,
                "provider_reply_chars": run_provider.reply_chars,
                "fixture_queries": list(result.queries or ())[:12],
                "fixture_fetches": result.source_urls[:12],
                "model_actions": model_actions[:60],
                "tool_calls": tool_calls[:60],
                "info": infos[:16],
                "summary_chars": len(result.summary or ""),
                "summary_preview": connector._clip(result.summary, 1600),
            }
            row["score"] = score_followup_quality_row(row)
            bind_row_evidence_refs(row, layout=layout, tracing_provider=run_provider)
            if trace is not None:
                trace.record_case_complete(case=case.name, arm=arm, row=row)
            return row
        except Exception as exc:
            provider_failure = connector._provider_failure_payload(provider)
            row = {
                "provider": provider_id,
                "case": case.name,
                "arm": arm,
                "ok": False,
                "seconds": round(time.time() - started, 3),
                "error": f"{type(exc).__name__}: {exc}",
                "provider_send_count": run_provider.send_index,
                "provider_reply_count": run_provider.reply_count,
                "model_actions": model_actions[:60],
                "tool_calls": tool_calls[:60],
                "info": infos[:16],
            }
            if provider_failure:
                row["provider_failure"] = provider_failure
            if trace is not None:
                trace.record_case_complete(case=case.name, arm=arm, row=row)
            return row
        finally:
            connector._detach_search_provider(search)
            store.close()


def run_provider(
    provider_id: str,
    *,
    cases: Sequence[connector.Case],
    arms: Sequence[str],
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
    pending = store.pending_keys(cases=cases, arms=arms, rerun_failed=rerun_failed)
    run_id = output.stem
    if trace is not None:
        trace.record_run_start(
            cases=tuple(case.name for case in cases),
            arms=tuple(arms),
            max_turns=max_turns,
            resumed_attempt=trace.event_count > 0,
        )
    if not pending:
        store.write(complete=True, extra={"finished_at": timestamp(), "stop_reason": "already_complete"})
        print(
            f"[{provider_id}] no pending rows for cases={','.join(case.name for case in cases)} "
            f"arms={','.join(arms)}; use --rerun-failed or a new --output to run again.",
            flush=True,
        )
        return store.payload
    store.write(complete=False)
    provider_controls.begin_task_context(f"research-followup-quality-ab:{provider_id}")
    provider = None
    try:
        provider = connector.TimedProvider(
            connect_provider(
                provider_id,
                port=port,
                open_if_missing=open_if_missing,
                bring_to_front=open_if_missing,
            ),
            send_timeout=send_timeout,
            new_chat_timeout=new_chat_timeout,
        )
        for case_name, arm, _repeat in pending:
            case = CASES[case_name]
            row = run_case(
                provider,
                provider_id=provider_id,
                case=case,
                arm=arm,
                max_turns=max_turns,
                run_id=run_id,
                trace=trace,
                layout=layout,
            )
            store.upsert(row, complete=False)
            print(
                f"[{provider_id} {case.name} {arm}] "
                f"ok={row.get('ok')} score={row.get('score')} "
                f"followup={row.get('followup_rounds')} "
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
            try:
                provider.close()
            except Exception:
                pass


def _default_output(provider_id: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return RESULTS_DIR / f"research_followup_quality_ab-{provider_id}-{stamp}.json"


def _parse_names(raw_values: list[str] | None, allowed: Mapping[str, object] | Sequence[str], label: str) -> tuple[str, ...]:
    if not raw_values:
        return tuple(allowed)
    names: list[str] = []
    for raw in raw_values:
        for part in str(raw or "").split(","):
            name = part.strip()
            if name:
                names.append(name)
    allowed_names = set(allowed)
    unknown = [name for name in names if name not in allowed_names]
    if unknown:
        raise SystemExit(f"unknown {label}: {', '.join(unknown)}")
    return tuple(dict.fromkeys(names))


def _self_test() -> None:
    rows = [
        {
            "case": "pubmed",
            "arm": "baseline",
            "ok": True,
            "score": 5,
            "proof_answer_status": "partial",
            "proof_coverage": 0.55,
            "unsupported_claim_rate": 0.2,
            "record_source_count": 1,
            "record_evidence_count": 1,
            "provider_send_count": 5,
            "seconds": 30.0,
        },
        {
            "case": "pubmed",
            "arm": "planner",
            "ok": True,
            "score": 6,
            "proof_answer_status": "partial",
            "proof_coverage": 0.67,
            "unsupported_claim_rate": 0.1,
            "record_source_count": 2,
            "record_evidence_count": 2,
            "provider_send_count": 6,
            "seconds": 40.0,
            "followup_rounds": 1,
            "planner_stop_reason": "max_followup_rounds",
        },
    ]
    summary = summarize(rows)
    usefulness = summary["by_case"]["pubmed"]["followup_usefulness"]
    assert summary["by_case"]["pubmed"]["delta"] == 1
    assert usefulness["useful"] is True
    assert gate_ok(rows, True) is True
    assert gate_ok(rows[:1], True) is False
    assert config_for_arm("baseline").enabled is False
    assert config_for_arm("planner").enabled is True


def main() -> int:
    parser = argparse.ArgumentParser(description="Live connector-backed Research follow-up quality A/B")
    parser.add_argument("--provider", choices=(*WEB_PROVIDERS, "all"), default="mimo")
    parser.add_argument("--case", action="append", help="case name or comma list; defaults to pubmed,arxiv")
    parser.add_argument("--arms", default=DEFAULT_ARMS)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--trace-output", type=Path)
    parser.add_argument("--max-turns", type=int, default=18)
    parser.add_argument("--send-timeout", type=float, default=180)
    parser.add_argument("--new-chat-timeout", type=float, default=90)
    parser.add_argument("--open-if-missing", action="store_true")
    parser.add_argument("--rerun-failed", action="store_true")
    parser.add_argument("--no-live-trace", action="store_true")
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
        trace: ABJournalWriter | None = None
        if not args.no_live_trace:
            try:
                trace = ABJournalWriter(
                    directory=layout.journal_dir,
                    experiment_id=PROBE,
                    run_id=output.stem,
                    provider=provider_id,
                    transcript_cache=TranscriptReplayCache(layout.journal_dir, mode=TRANSCRIPT_MODE_DIGEST_ONLY),
                )
            except ABJournalIdentityMismatch as exc:
                print(str(exc), file=sys.stderr)
                return 2
        try:
            result_payload = run_provider(
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
            ok = bool(result_payload.get("ok"))
            write_arm_manifest(
                output,
                build_arm_manifest(
                    suite=PROBE,
                    provider=provider_id,
                    arms=selected_arms,
                    cases=[case.name for case in selected_cases],
                    max_turns=max(1, args.max_turns),
                    journal_dir=layout.journal_dir,
                    transcript_mode="digest-only" if not args.no_live_trace else "off",
                    started_at=str(result_payload.get("started_at") or ""),
                    finished_at=str(result_payload.get("finished_at") or timestamp()),
                    stop_reason=str(result_payload.get("stop_reason") or ("done" if ok else "unknown")),
                    codey_failure_class=(AB_FAILURE_NONE if ok else AB_FAILURE_CODEY),
                ),
            )
        finally:
            if trace is not None:
                trace.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
