"""Live A/B probe for done-stage boundary tuning on connector runs.

This probe does not change production code. It reuses the live source connector
Research flow, but wraps the model-facing prompt with an experimental overlay
that makes the final citation boundary harder:
- only citable sources may appear in done.answer or 来源
- opened-but-not-citable sources are warning-only
- quality retry prompts get a short checklist instead of a vague nudge

The goal is to measure whether that prompt shape reduces done retries and
quality-review loops.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey import provider_controls
from codey.knowledge.store import KnowledgeStore
from codey.research.browser_search import BrowserSearchProvider
from codey.research.connector_search import ConnectorAwareSearchProvider
from codey.research import report_quality as report_quality_module
from codey.research import runner as runner_module
from codey.research.ledger import EvidenceItem, ResearchLedger
from codey.research.proof_quality import review_research_proof
from codey.research.protocols import exact_json_object
from codey.research.source_document import SourceDocument
from codey.research.runner import ResearchRunner
from codey.providers.registry import connect_provider, provider_ids

from tests.manual import source_connector_ab as base

RESULTS_DIR = Path(__file__).resolve().parent / "results"
WEB_PROVIDERS = tuple(provider_id for provider_id in provider_ids() if provider_id != "local")
ARMS = ("baseline", "boundary", "batch", "finalizer")
DEFAULT_ARMS = "baseline,finalizer"
BOUNDARY_ARMS = {"boundary", "finalizer"}
QUALITY_CHECKLIST_ARMS = {"boundary", "batch", "finalizer"}
BATCH_REVIEW_ARMS = {"batch", "finalizer"}
FINALIZER_ARMS = {"finalizer"}
MAX_RESULT_BYTES = base.MAX_RESULT_BYTES
MAX_TRACE_BYTES = base.MAX_TRACE_BYTES * 4
TRACE_PROMPT_CHARS = base.TRACE_PROMPT_CHARS
TRACE_REPLY_CHARS = base.TRACE_REPLY_CHARS

CASES = {
    "pubmed": base.CASES["pubmed"],
    "arxiv": base.CASES["arxiv"],
}


_BOUNDARY_APPENDIX = """Probe done boundary reminder:
- In done.answer and 来源, use only evidence-backed citable sources.
- The final ## 来源 section must use exact numbered lines: [1] Title - https://final-url
- Every [n] citation in the report body must have a matching [n] line in 来源.
- Opened-but-not-citable sources are warning-only; never number them in done.answer or 来源.
- If a source is opened but not citable, mention it only as an unnumbered limitation.
"""

_QUALITY_CHECKLIST = """Probe quality-review checklist:
1. Rebuild the full report, not just the 来源 section.
2. Rewrite 来源 as exact lines like [1] Title - https://final-url.
3. Every [n] in 结论 and 关键证据 must appear in 来源.
4. Every item in 来源 must be citable and have saved evidence.
5. Remove any [n] attached to an opened-only source.
6. If a source cannot be cited, mention it only as an unnumbered limitation.
"""

_SOURCE_ID_REF_RE = re.compile(r"\[\[?(s\d+)([^\]\[]*)\]\]?", re.IGNORECASE)
_NUMERIC_REF_RE = re.compile(r"(?<![A-Za-z0-9_!])\[(\d+)([^\]]*)\]")


@dataclass
class DoneFinalizerStats:
    attempts: int = 0
    rewrites: int = 0
    errors: int = 0
    generated_sources: int = 0
    added_citations: int = 0
    last_reason: str = ""


@dataclass(frozen=True)
class FinalizedAnswer:
    text: str
    changed: bool = False
    source_count: int = 0
    added_citations: int = 0
    reason: str = ""


class DoneFinalizer:
    def __init__(self) -> None:
        self.runner: ResearchRunner | None = None
        self.stats = DoneFinalizerStats()

    def bind(self, runner: ResearchRunner) -> None:
        self.runner = runner

    def rewrite_reply(
        self,
        reply: str,
        *,
        trace: base.LiveTrace | None,
        run_id: str,
        provider: str,
        provider_name: str,
        case: str,
        arm: str,
        sample: int,
        turn: int,
    ) -> str:
        obj, _kind, error = exact_json_object(reply or "")
        if error or set(obj.keys()) != {"tool", "args"}:
            return reply
        if str(obj.get("tool") or "").strip().lower() != "done":
            return reply
        args = obj.get("args")
        if not isinstance(args, dict):
            return reply
        answer = str(args.get("answer") or "")
        if not answer.strip() or self.runner is None:
            return reply
        self.stats.attempts += 1
        try:
            source_ids = _source_id_url_map(getattr(self.runner, "controller", None))
            finalized = finalize_done_answer(answer, self.runner.tools.ledger, source_ids=source_ids)
        except Exception as exc:
            self.stats.errors += 1
            self.stats.last_reason = f"{type(exc).__name__}: {exc}"
            return reply
        self.stats.generated_sources = max(self.stats.generated_sources, finalized.source_count)
        self.stats.added_citations += finalized.added_citations
        self.stats.last_reason = finalized.reason
        if not finalized.changed:
            return reply
        self.stats.rewrites += 1
        rewritten_args = dict(args)
        rewritten_args["answer"] = finalized.text
        rewritten = json.dumps({"tool": "done", "args": rewritten_args}, ensure_ascii=False)
        if trace is not None:
            trace.record({
                "event": "finalizer_rewrite",
                "run_id": run_id,
                "provider": provider,
                "provider_name": provider_name,
                "case": case,
                "arm": arm,
                "sample": sample,
                "turn": turn,
                "source_count": finalized.source_count,
                "added_citations": finalized.added_citations,
                "reason": finalized.reason,
                "raw_reply": base._clip(reply, TRACE_REPLY_CHARS),
                "rewritten_reply": base._clip(rewritten, TRACE_REPLY_CHARS),
            })
        return rewritten


def finalize_done_answer(
    answer: str,
    ledger: ResearchLedger,
    *,
    source_ids: dict[str, str] | None = None,
) -> FinalizedAnswer:
    citable_urls = _citable_urls(ledger)
    if not citable_urls:
        return FinalizedAnswer(answer, reason="no_citable_sources")
    source_ids = source_ids or {}
    sections = report_quality_module.parse_sections(answer)
    if not sections:
        return FinalizedAnswer(answer, reason="no_report_sections")
    number_for_url = {url: index for index, url in enumerate(citable_urls, 1)}
    source_id_to_number = {
        source_id.lower(): number_for_url[url]
        for source_id, url in source_ids.items()
        if url in number_for_url
    }
    old_number_to_new = _old_source_numbers(sections.get("sources", ""), ledger, number_for_url)
    added_citations = 0
    rewritten: dict[str, str] = {}
    for key in report_quality_module.REQUIRED_SECTIONS:
        if key == "sources":
            continue
        body = sections.get(key, "")
        if not body.strip():
            continue
        body = _rewrite_source_id_refs(body, source_id_to_number)
        body = _rewrite_numeric_refs(body, old_number_to_new, len(citable_urls))
        if key in {"conclusion", "evidence", "source_quality"}:
            body, added = _ensure_citation(body, 1)
            added_citations += int(added)
        elif key == "counter" and not report_quality_module._says_no_strong_counter(body):
            body, added = _ensure_citation(body, 1)
            added_citations += int(added)
        rewritten[key] = body
    rewritten["sources"] = _render_sources(citable_urls, ledger)
    compiled = _render_report(rewritten)
    changed = _normalized_report(compiled) != _normalized_report(answer)
    reason = "compiled_citations" if changed else "already_compiled"
    return FinalizedAnswer(
        compiled if changed else answer,
        changed=changed,
        source_count=len(citable_urls),
        added_citations=added_citations,
        reason=reason,
    )


def _citable_urls(ledger: ResearchLedger) -> list[str]:
    final_urls = ledger.final_url_set()
    urls: list[str] = []
    for item in ledger.evidence_items:
        url = str(item.source_url or "").strip()
        canonical = ledger.canonical_opened_url(url) or url
        if canonical in final_urls and canonical not in urls:
            urls.append(canonical)
    return urls


def _source_id_url_map(controller: object) -> dict[str, str]:
    value = getattr(controller, "_source_urls_by_id", {}) if controller is not None else {}
    if not isinstance(value, dict):
        return {}
    return {str(key).lower(): str(url) for key, url in value.items() if str(key) and str(url)}


def _old_source_numbers(source_text: str, ledger: ResearchLedger, number_for_url: dict[str, int]) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for citation in report_quality_module.parse_citations(source_text, ledger):
        url = ledger.canonical_opened_url(citation.url) or citation.url
        if url in number_for_url:
            mapping[int(citation.number)] = number_for_url[url]
    return mapping


def _rewrite_source_id_refs(text: str, source_id_to_number: dict[str, int]) -> str:
    def repl(match: re.Match[str]) -> str:
        number = source_id_to_number.get(match.group(1).lower())
        return f"[{number}{match.group(2)}]" if number else match.group(0)

    return _SOURCE_ID_REF_RE.sub(repl, text)


def _rewrite_numeric_refs(text: str, old_number_to_new: dict[int, int], source_count: int) -> str:
    def repl(match: re.Match[str]) -> str:
        old_number = int(match.group(1))
        new_number = old_number_to_new.get(old_number)
        if new_number is None and 1 <= old_number <= source_count:
            new_number = old_number
        if new_number is None and source_count == 1:
            new_number = 1
        return f"[{new_number}{match.group(2)}]" if new_number is not None else match.group(0)

    return _NUMERIC_REF_RE.sub(repl, text)


def _ensure_citation(text: str, number: int) -> tuple[str, bool]:
    if report_quality_module.citation_refs(text):
        return text, False
    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        lines[index] = line.rstrip() + f" [{number}]"
        return "\n".join(lines), True
    return text, False


def _render_sources(urls: list[str], ledger: ResearchLedger) -> str:
    lines: list[str] = []
    for index, url in enumerate(urls, 1):
        title = _source_title(ledger, url)
        lines.append(f"[{index}] {title} - {url}")
    return "\n".join(lines)


def _source_title(ledger: ResearchLedger, url: str) -> str:
    title = ledger.source_title(url).strip() or "Source"
    return re.sub(r"\s+", " ", title).strip(" -–—:：") or "Source"


def _render_report(sections: dict[str, str]) -> str:
    parts: list[str] = []
    for key in report_quality_module.REQUIRED_SECTIONS:
        body = str(sections.get(key) or "").strip()
        if not body and key != "sources":
            continue
        parts.append(f"## {report_quality_module._section_title(key)}\n{body}".rstrip())
    return "\n\n".join(parts).strip()


def _normalized_report(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


@contextlib.contextmanager
def _patched_quality_review(enabled: bool):
    if not enabled:
        yield
        return
    original = runner_module.review_report_quality
    runner_module.review_report_quality = _batched_quality_review(original)
    try:
        yield
    finally:
        runner_module.review_report_quality = original


def _batched_quality_review(original: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(
        summary: str,
        *,
        ledger: ResearchLedger,
        opened_sources: set[str],
        search_result_urls: set[str],
    ):
        review = original(
            summary,
            ledger=ledger,
            opened_sources=opened_sources,
            search_result_urls=search_result_urls,
        )
        if review.ok:
            return review
        blockers = _quality_blockers(
            summary,
            ledger=ledger,
            opened_sources=opened_sources,
            search_result_urls=search_result_urls,
            fallback=review.message,
        )
        if not blockers:
            return review
        message = "Report quality failed with multiple blocker(s):\n" + "\n".join(
            f"- {item}" for item in blockers[:8]
        )
        return report_quality_module.ReportQualityReview(
            False,
            message,
            sections=report_quality_module.parse_sections(summary),
        )

    return wrapped


def _quality_blockers(
    summary: str,
    *,
    ledger: ResearchLedger,
    opened_sources: set[str],
    search_result_urls: set[str],
    fallback: str,
) -> list[str]:
    sections = report_quality_module.parse_sections(summary)
    blockers: list[str] = []
    missing = [key for key in report_quality_module.REQUIRED_SECTIONS if not sections.get(key, "").strip()]
    if missing:
        blockers.append(
            "missing required section(s): "
            + ", ".join(report_quality_module._section_title(item) for item in missing)
        )
        return blockers
    strict_problem = report_quality_module.provenance_problem(
        report_quality_module._strict_provenance_text(sections, summary),
        opened_sources=opened_sources,
        search_result_urls=search_result_urls,
    )
    if strict_problem:
        blockers.append(strict_problem)
    context_problem = report_quality_module.provenance_problem(
        report_quality_module._context_provenance_text(sections),
        opened_sources=opened_sources,
        search_result_urls=search_result_urls,
        allow_search_result_mentions=True,
    )
    if context_problem:
        blockers.append(context_problem)
    if not ledger.final_url_set():
        blockers.append("no opened source is available for citation")

    body_ref_items = report_quality_module.citation_ref_items(report_quality_module._without_sources(sections))
    pages_by_number = report_quality_module._pages_by_number(body_ref_items)
    citations = [
        replace(item, pages=pages_by_number.get(item.number, ()))
        for item in report_quality_module.parse_citations(sections.get("sources", ""), ledger)
    ]
    if not citations:
        blockers.append("来源 section must list numbered sources like [1] Title - https://final-url")
    source_numbers = {item.number for item in citations}
    body_refs = {item.number for item in body_ref_items}
    missing_sources = sorted(body_refs - source_numbers)
    if missing_sources:
        blockers.append(
            "citation number(s) appear in the report but not in 来源: "
            + ", ".join(f"[{item}]" for item in missing_sources[:6])
        )
    final_urls = ledger.final_url_set()
    unopened = [item.url for item in citations if item.url not in final_urls]
    if unopened:
        blockers.append("来源 URL(s) were not opened as final URLs: " + ", ".join(unopened[:3]))
    evidence_urls = {
        str(item.get("source_url") or "")
        for item in ledger.evidence_payload()
        if str(item.get("source_url") or "") and str(item.get("excerpt") or "").strip()
    }
    missing_evidence = sorted({item.url for item in citations} - evidence_urls)
    if missing_evidence:
        blockers.append(
            "every cited source needs saved evidence; missing: " + ", ".join(missing_evidence[:3])
        )
    page_problem = report_quality_module._page_citation_problem(citations, ledger) if citations else ""
    if page_problem:
        blockers.append(page_problem)
    if not report_quality_module.citation_refs(sections.get("conclusion", "")):
        blockers.append("结论 section must cite evidence with [n]")
    if not report_quality_module.citation_refs(sections.get("evidence", "")):
        blockers.append("关键证据 section must cite opened sources with [n]")
    counter = sections.get("counter", "")
    if not report_quality_module.citation_refs(counter) and not report_quality_module._says_no_strong_counter(counter):
        blockers.append("反证与限制 must cite counter-evidence with [n] or explicitly say 未找到强反证")
    if not blockers and fallback:
        blockers.append(str(fallback).removeprefix("Report quality failed: ").strip())
    return list(dict.fromkeys(item for item in blockers if item))


class PromptBoundaryProvider:
    def __init__(
        self,
        provider,
        *,
        trace: "LiveTrace | None",
        run_id: str,
        provider_id: str,
        provider_name: str,
        case: str,
        arm: str,
        sample: int,
        finalizer: "DoneFinalizer | None" = None,
    ) -> None:
        self.provider = provider
        self.trace = trace
        self.run_id = run_id
        self.provider_id = provider_id
        self.provider_name = provider_name
        self.case = case
        self.arm = arm
        self.sample = int(sample)
        self.finalizer = finalizer
        self.send_index = 0
        self.name = getattr(provider, "name", "")
        self.location = getattr(provider, "location", "")
        self.thread_safe_send = getattr(provider, "thread_safe_send", False)
        self.overlay_hits = 0
        self.boundary_overlay_hits = 0
        self.quality_overlay_hits = 0

    def __getattr__(self, name: str):
        return getattr(self.provider, name)

    def new_chat(self, timeout=None) -> None:
        return self.provider.new_chat(timeout=timeout)

    def _overlay_prompt(self, text: str) -> tuple[str, list[str]]:
        prompt = str(text or "")
        if self.arm not in BOUNDARY_ARMS and self.arm not in QUALITY_CHECKLIST_ARMS:
            return prompt, []
        additions: list[str] = []
        if self.arm in BOUNDARY_ARMS and _needs_boundary_overlay(prompt):
            additions.append(_BOUNDARY_APPENDIX)
            self.boundary_overlay_hits += 1
        if self.arm in QUALITY_CHECKLIST_ARMS and _needs_quality_checklist(prompt):
            additions.append(_QUALITY_CHECKLIST)
            self.quality_overlay_hits += 1
        if additions:
            self.overlay_hits += 1
            prompt = prompt + "\n\n" + "\n\n".join(additions)
        return prompt, additions

    def send(self, text: str, timeout=None) -> str:
        prompt, _ = self._overlay_prompt(text)
        self.send_index += 1
        turn = self.send_index
        if self.trace is not None:
            self.trace.record_send_start(
                run_id=self.run_id,
                provider=self.provider_id,
                provider_name=self.provider_name,
                case=self.case,
                arm=self.arm,
                sample=self.sample,
                turn=turn,
                prompt=prompt,
            )
        try:
            reply = self.provider.send(prompt, timeout=timeout)
        except Exception as exc:
            if self.trace is not None:
                event = {
                    "event": "send_error",
                    "run_id": self.run_id,
                    "provider": self.provider_id,
                    "provider_name": self.provider_name,
                    "case": self.case,
                    "arm": self.arm,
                    "sample": self.sample,
                    "turn": turn,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                failure = base._provider_failure_payload(self.provider)
                if failure:
                    event["provider_failure"] = failure
                self.trace.record(event)
            raise
        raw_reply = str(reply or "")
        final_reply = raw_reply
        if self.finalizer is not None:
            final_reply = self.finalizer.rewrite_reply(
                raw_reply,
                trace=self.trace,
                run_id=self.run_id,
                provider=self.provider_id,
                provider_name=self.provider_name,
                case=self.case,
                arm=self.arm,
                sample=self.sample,
                turn=turn,
            )
        if self.trace is not None:
            self.trace.record_reply(
                run_id=self.run_id,
                provider=self.provider_id,
                provider_name=self.provider_name,
                case=self.case,
                arm=self.arm,
                sample=self.sample,
                turn=turn,
                prompt=prompt,
                reply=final_reply,
            )
        return final_reply

    def close(self) -> None:
        return self.provider.close()


def run_case(
    provider,
    *,
    provider_id: str,
    case: base.Case,
    arm: str,
    sample: int,
    max_turns: int,
    run_id: str,
    trace: base.LiveTrace | None,
) -> dict[str, Any]:
    started = time.time()
    tool_calls: list[dict[str, Any]] = []
    model_actions: list[dict[str, Any]] = []
    infos: list[str] = []
    provider_name = str(getattr(provider, "name", "") or "")
    with tempfile.TemporaryDirectory(prefix="codey-source-connector-done-ab-") as td:
        store = KnowledgeStore(Path(td))
        search = _search_provider_for_arm(arm)
        try:
            if trace is not None:
                trace.record_case_start(
                    run_id=run_id,
                    provider=provider_id,
                    case=case.name,
                    arm=arm,
                    sample=sample,
                    question=case.question,
                )
            finalizer = DoneFinalizer() if arm in FINALIZER_ARMS else None
            run_provider = PromptBoundaryProvider(
                provider,
                trace=trace,
                run_id=run_id,
                provider_id=provider_id,
                provider_name=provider_name,
                case=case.name,
                arm=arm,
                sample=sample,
                finalizer=finalizer,
            )
            runner = ResearchRunner(
                run_provider,
                search,
                store,
                max_turns=max_turns,
                session_id=f"source-connector-done-ab-{provider_id}-{case.name}-{arm}",
                project="",
                run_id=run_id,
            )
            if finalizer is not None:
                finalizer.bind(runner)
            with _patched_quality_review(arm in BATCH_REVIEW_ARMS):
                for event in runner.run(case.question):
                    if event.kind == "turn":
                        model_actions.extend(base._safe_model_actions(event.turn, event.reply)[:3])
                    if event.kind == "info":
                        infos.append(str(event.message or "")[:240])
                    if event.kind == "tool" and event.call is not None:
                        tool_calls.append({
                            "turn": event.turn,
                            "name": event.call.name,
                            "args": base._safe_args(event.call.args),
                            "ok": bool(event.outcome.ok) if event.outcome is not None else False,
                            "status": event.outcome.presentation_status() if event.outcome is not None else "",
                        })
            if runner.result is None:
                raise RuntimeError("research finished without result")
            result = runner.result
            proof = (
                review_research_proof(result.research_record, question=case.question)
                if result.research_record is not None
                else None
            )
            done_attempts = sum(1 for item in model_actions if item.get("tool") == "done")
            quality_retry_count = sum(1 for item in infos if item.startswith("Report quality failed"))
            connector_errors = list(getattr(search, "last_connector_errors", []))[:8]
            opened_target_host = _opened_target_host(result.source_urls, case.target_hosts)
            eventual_done_passed = result.stop_reason == "done"
            first_done_passed = eventual_done_passed and done_attempts == 1 and quality_retry_count == 0
            proof_ok = bool(proof.ok) if proof is not None else False
            connector_valid = opened_target_host and not connector_errors
            row = {
                "provider": provider_id,
                "case": case.name,
                "arm": arm,
                "sample": sample,
                "ok": True,
                "seconds": round(time.time() - started, 3),
                "stop_reason": result.stop_reason,
                "turns": result.turns,
                "done_attempts": done_attempts,
                "quality_retry_count": quality_retry_count,
                "first_done_passed": first_done_passed,
                "eventual_done_passed": eventual_done_passed,
                "clean_success": first_done_passed and proof_ok,
                "connector_valid": connector_valid,
                "sources_read": result.sources_read,
                "opened_urls": result.source_urls[:12],
                "opened_target_host": opened_target_host,
                "evidence_count": len(result.evidence_items),
                "notes_created": len(result.notes_created),
                "connector_errors": connector_errors,
                "proof_ok": proof_ok,
                "proof_answer_status": proof.answer_status if proof is not None else "",
                "proof_missing_evidence": list(proof.missing_evidence[:8]) if proof is not None else [],
                "citation_map": result.citation_map[:8],
                "quality_warnings": result.quality_warnings[:8],
                "expected_terms_present": _expected_terms_present(result.summary, case.expected_terms),
                "summary_chars": len(result.summary or ""),
                "summary_text": base._clip(result.summary, 8000),
                "summary_preview": base._clip(result.summary, 1200),
                "overlay_hits": run_provider.overlay_hits,
                "boundary_overlay_hits": run_provider.boundary_overlay_hits,
                "quality_overlay_hits": run_provider.quality_overlay_hits,
                "finalizer_attempts": finalizer.stats.attempts if finalizer is not None else 0,
                "finalizer_rewrites": finalizer.stats.rewrites if finalizer is not None else 0,
                "finalizer_errors": finalizer.stats.errors if finalizer is not None else 0,
                "finalizer_generated_sources": finalizer.stats.generated_sources if finalizer is not None else 0,
                "finalizer_added_citations": finalizer.stats.added_citations if finalizer is not None else 0,
                "finalizer_reason": finalizer.stats.last_reason if finalizer is not None else "",
                "model_actions": model_actions[:40],
                "used_controller_open_action": any(
                    item.get("tool") in {"open_result", "reopen_source", "open_hit"}
                    for item in model_actions
                ),
                "used_legacy_open_url_id_action": any(
                    item.get("tool") == "open_url"
                    and any(key in item.get("args", {}) for key in ("result_id", "source_id", "hit_id"))
                    for item in model_actions
                ),
                "tool_calls": tool_calls[:40],
                "info": infos[:12],
            }
            row["score"] = _score_row(row)
            if trace is not None:
                trace.record_case_complete(
                    run_id=run_id,
                    provider=provider_id,
                    case=case.name,
                    arm=arm,
                    sample=sample,
                    row=row,
                )
            return row
        except Exception as exc:
            provider_failure = base._provider_failure_payload(provider)
            row = {
                "provider": provider_id,
                "case": case.name,
                "arm": arm,
                "sample": sample,
                "ok": False,
                "seconds": round(time.time() - started, 3),
                "error": f"{type(exc).__name__}: {exc}",
                "model_actions": model_actions[:40],
                "tool_calls": tool_calls[:40],
                "info": infos[:12],
            }
            if provider_failure:
                row["provider_failure"] = provider_failure
            if trace is not None:
                trace.record_case_complete(
                    run_id=run_id,
                    provider=provider_id,
                    case=case.name,
                    arm=arm,
                    sample=sample,
                    row=row,
                )
            return row
        finally:
            base._detach_search_provider(search)
            store.close()


def run_provider(
    provider_id: str,
    *,
    cases: tuple[base.Case, ...],
    arms: tuple[str, ...],
    samples: int,
    port: int,
    output: Path,
    max_turns: int,
    send_timeout: float,
    new_chat_timeout: float,
    open_if_missing: bool,
    rerun_failed: bool,
    trace: base.LiveTrace | None,
    run_id: str,
) -> dict[str, Any]:
    try:
        payload = _load_or_new_payload(output, provider_id=provider_id, cases=cases, arms=arms, samples=samples)
    except base.OutputProviderMismatch as exc:
        if trace is not None:
            trace.record({
                "event": "provider_mismatch",
                "run_id": run_id,
                "provider": provider_id,
                "output": str(output),
                "expected_provider": exc.expected,
                "found_provider": exc.found,
            })
        raise
    payload["trace_output"] = str(trace.path) if trace is not None else ""
    existing = {
        (str(row.get("case") or ""), str(row.get("arm") or ""), int(row.get("sample") or 1))
        for row in payload["rows"]
        if row.get("ok") or not rerun_failed
    }
    pending = _pending_case_sample_keys(cases=cases, arms=arms, samples=samples, existing=existing)
    if trace is not None:
        trace.record_run_start(
            run_id=run_id,
            provider=provider_id,
            trace_output=str(trace.path),
            cases=tuple(case.name for case in cases),
            arms=arms,
            samples=samples,
            max_turns=max_turns,
        )
    if not pending:
        if trace is not None:
            trace.record({
                "event": "no_pending_rows",
                "run_id": run_id,
                "provider": provider_id,
                "output": str(output),
                "cases": [case.name for case in cases],
                "arms": list(arms),
                "samples": max(1, int(samples)),
                "rerun_failed": bool(rerun_failed),
                "existing_rows": len(payload["rows"]),
            })
            trace.record_run_complete(run_id=run_id, provider=provider_id, rows=len(payload["rows"]))
        print(
            f"[{provider_id}] no pending rows for cases={','.join(case.name for case in cases)} "
            f"arms={','.join(arms)} samples={max(1, int(samples))}; "
            "use --rerun-failed or a new --output to run again.",
            flush=True,
        )
        return payload
    _write_payload(output, payload)
    provider_controls.begin_task_context(f"source-connector-done-ab:{provider_id}")
    provider = None
    try:
        provider = base.TimedProvider(
            connect_provider(
                provider_id,
                port=port,
                open_if_missing=open_if_missing,
                bring_to_front=open_if_missing,
            ),
            send_timeout=send_timeout,
            new_chat_timeout=new_chat_timeout,
        )
        for sample in range(1, max(1, int(samples)) + 1):
            for case in cases:
                for arm in arms:
                    key = (case.name, arm, sample)
                    if key in existing:
                        continue
                    row = run_case(
                        provider,
                        provider_id=provider_id,
                        case=case,
                        arm=arm,
                        sample=sample,
                        max_turns=max_turns,
                        run_id=run_id,
                        trace=trace,
                    )
                    payload["rows"].append(row)
                    payload["summary"] = summarize(payload["rows"])
                    payload["updated_at"] = _timestamp()
                    _write_payload(output, payload)
                    print(
                        f"[{provider_id} sample={sample} {case.name} {arm}] "
                        f"ok={row.get('ok')} score={row.get('score')} done_attempts={row.get('done_attempts')} "
                        f"quality_retries={row.get('quality_retry_count')} stop={row.get('stop_reason', row.get('error', ''))}",
                        flush=True,
                    )
        payload["complete"] = True
        payload["summary"] = summarize(payload["rows"])
        payload["updated_at"] = _timestamp()
        _write_payload(output, payload)
        if trace is not None:
            trace.record_run_complete(
                run_id=run_id,
                provider=provider_id,
                rows=len(payload["rows"]),
            )
        return payload
    finally:
        provider_controls.end_task_context()
        if provider is not None:
            try:
                provider.close()
            except Exception:
                pass


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"rows": len(rows), "run_count": len(rows), "by_case": {}, "by_arm": {}}
    for arm in sorted({str(row.get("arm") or "") for row in rows if row.get("arm")}):
        arm_rows = [row for row in rows if row.get("arm") == arm]
        summary["by_arm"][arm] = _aggregate_rows(arm_rows)
    for case in sorted({str(row.get("case") or "") for row in rows if row.get("case")}):
        case_rows = [row for row in rows if row.get("case") == case]
        arms = {
            arm: [row for row in case_rows if row.get("arm") == arm]
            for arm in sorted({str(row.get("arm") or "") for row in case_rows if row.get("arm")})
        }
        baseline_rows = arms.get("baseline", [])
        boundary_rows = arms.get("boundary", [])
        finalizer_rows = arms.get("finalizer", [])
        summary["by_case"][case] = {
            "runs": len(case_rows),
            "arms": {arm: _aggregate_rows(arm_rows) for arm, arm_rows in arms.items()},
            "paired_vs_baseline": _paired_vs_baseline(case_rows),
            "baseline_first_pass_rate": _rate(baseline_rows, "first_done_passed"),
            "boundary_first_pass_rate": _rate(boundary_rows, "first_done_passed"),
            "finalizer_first_pass_rate": _rate(finalizer_rows, "first_done_passed"),
            "baseline_eventual_success_rate": _rate(baseline_rows, "eventual_done_passed"),
            "boundary_eventual_success_rate": _rate(boundary_rows, "eventual_done_passed"),
            "finalizer_eventual_success_rate": _rate(finalizer_rows, "eventual_done_passed"),
        }
    return summary


def _aggregate_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(rows)
    return {
        "count": count,
        "first_pass_rate": _rate(rows, "first_done_passed"),
        "eventual_success_rate": _rate(rows, "eventual_done_passed"),
        "clean_success_rate": _rate(rows, "clean_success"),
        "proof_pass_rate": _rate(rows, "proof_ok"),
        "connector_valid_rate": _rate(rows, "connector_valid"),
        "average_done_attempts": _avg(rows, "done_attempts"),
        "average_quality_retry_count": _avg(rows, "quality_retry_count"),
        "average_score": _avg(rows, "score"),
        "average_finalizer_rewrites": _avg(rows, "finalizer_rewrites"),
        "average_finalizer_added_citations": _avg(rows, "finalizer_added_citations"),
    }


def _paired_vs_baseline(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_sample: dict[int, dict[str, dict[str, Any]]] = {}
    for row in rows:
        sample = int(row.get("sample") or 1)
        arm = str(row.get("arm") or "")
        if not arm:
            continue
        by_sample.setdefault(sample, {})[arm] = row
    arms = sorted({str(row.get("arm") or "") for row in rows if row.get("arm") and row.get("arm") != "baseline"})
    summary: dict[str, Any] = {}
    for arm in arms:
        pairs = [(items.get("baseline"), items.get(arm)) for items in by_sample.values()]
        pairs = [(base_row, arm_row) for base_row, arm_row in pairs if base_row and arm_row]
        if not pairs:
            continue
        summary[arm] = {
            "paired_samples": len(pairs),
            "score_delta_avg": _avg_deltas(pairs, "score"),
            "done_attempt_delta_avg": _avg_deltas(pairs, "done_attempts"),
            "quality_retry_delta_avg": _avg_deltas(pairs, "quality_retry_count"),
            "first_pass_delta": _rate([arm_row for _base_row, arm_row in pairs], "first_done_passed")
            - _rate([base_row for base_row, _arm_row in pairs], "first_done_passed"),
        }
    return summary


def _avg_deltas(pairs: list[tuple[dict[str, Any], dict[str, Any]]], key: str) -> float | None:
    deltas: list[float] = []
    for base_row, arm_row in pairs:
        try:
            deltas.append(float(arm_row.get(key) or 0) - float(base_row.get(key) or 0))
        except (TypeError, ValueError):
            continue
    if not deltas:
        return None
    return round(sum(deltas) / len(deltas), 3)


def _rate(rows: list[dict[str, Any]], key: str) -> float | None:
    if not rows:
        return None
    return round(sum(1 for row in rows if row.get(key)) / len(rows), 3)


def _avg(rows: list[dict[str, Any]], key: str) -> float | None:
    values: list[float] = []
    for row in rows:
        value = row.get(key)
        if isinstance(value, bool) or value in (None, ""):
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _search_provider_for_arm(arm: str):
    base_search = BrowserSearchProvider(isolated=False, bring_to_front=False)
    return ConnectorAwareSearchProvider(base_search)


def _opened_target_host(urls: list[str], target_hosts: tuple[str, ...]) -> bool:
    if not target_hosts:
        return True
    targets = {host.lower().removeprefix("www.") for host in target_hosts}
    for url in urls:
        try:
            host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        except ValueError:
            host = ""
        if host in targets:
            return True
    return False


def _expected_terms_present(summary: str, expected_terms: tuple[str, ...]) -> bool:
    text = str(summary or "").casefold()
    return all(term.casefold() in text for term in expected_terms)


def _score_row(row: dict[str, Any]) -> int:
    return (
        (3 if row.get("stop_reason") == "done" else 0)
        + (3 if row.get("opened_target_host") else 0)
        + (2 if int(row.get("evidence_count") or 0) > 0 else 0)
        + (2 if row.get("proof_ok") else 0)
        + (1 if row.get("expected_terms_present") else 0)
    )


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _needs_boundary_overlay(prompt: str) -> bool:
    text = str(prompt or "")
    if _needs_quality_checklist(text):
        return True
    return _done_allowed_in_prompt(text)


def _needs_quality_checklist(prompt: str) -> bool:
    text = str(prompt or "")
    return (
        "Your last done.answer did not pass the research quality review" in text
        or "Report quality failed" in text
    )


def _done_allowed_in_prompt(prompt: str) -> bool:
    for line in str(prompt or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- Allowed tools this turn:"):
            continue
        tools = {item.strip() for item in stripped.partition(":")[2].split(",")}
        return "done" in tools
    return False


def _load_or_new_payload(
    output: Path,
    *,
    provider_id: str,
    cases: tuple[base.Case, ...],
    arms: tuple[str, ...],
    samples: int,
) -> dict[str, Any]:
    if output.exists():
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
                base._ensure_payload_provider(payload, provider_id=provider_id, output=output)
                payload["complete"] = False
                payload["probe"] = "source_connector_done_ab"
                payload["samples"] = max(int(payload.get("samples") or 1), int(samples or 1))
                return payload
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "probe": "source_connector_done_ab",
        "provider": provider_id,
        "started_at": _timestamp(),
        "updated_at": _timestamp(),
        "trace_output": "",
        "cases": [case.name for case in cases],
        "arms": list(arms),
        "samples": max(1, int(samples or 1)),
        "complete": False,
        "rows": [],
        "summary": {},
    }


def _pending_case_sample_keys(
    *,
    cases: tuple[base.Case, ...],
    arms: tuple[str, ...],
    samples: int,
    existing: set[tuple[str, str, int]],
) -> list[tuple[str, str, int]]:
    return [
        (case.name, arm, sample)
        for sample in range(1, max(1, int(samples)) + 1)
        for case in cases
        for arm in arms
        if (case.name, arm, sample) not in existing
    ]


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
        raise ValueError("source connector done A/B result exceeded bounded size")
    base._write_json_atomic(path, payload)


def _default_output(provider_id: str) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return RESULTS_DIR / f"source_connector_done_ab-{provider_id}-{stamp}.json"


def _trace_output_path(output: Path) -> Path:
    if output.suffix:
        return output.with_name(f"{output.stem}.trace.json")
    return output.with_name(f"{output.name}.trace.json")


class LiveTrace(base.LiveTrace):
    def record(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        payload["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        self.events.append(payload)
        self.flush()

    def record_run_start(
        self,
        *,
        run_id: str,
        provider: str,
        trace_output: str,
        cases: tuple[str, ...],
        arms: tuple[str, ...],
        samples: int,
        max_turns: int,
    ) -> None:
        self.record({
            "event": "run_start",
            "run_id": run_id,
            "provider": provider,
            "trace_output": trace_output,
            "cases": list(cases),
            "arms": list(arms),
            "samples": samples,
            "max_turns": max_turns,
        })

    def record_case_start(
        self,
        *,
        run_id: str,
        provider: str,
        case: str,
        arm: str,
        sample: int,
        question: str,
    ) -> None:
        self.record({
            "event": "case_start",
            "run_id": run_id,
            "provider": provider,
            "case": case,
            "arm": arm,
            "sample": sample,
            "question": base._clip(question, 1200),
        })

    def record_send_start(
        self,
        *,
        run_id: str,
        provider: str,
        provider_name: str,
        case: str,
        arm: str,
        sample: int,
        turn: int,
        prompt: str,
    ) -> None:
        self.record({
            "event": "send_start",
            "run_id": run_id,
            "provider": provider,
            "provider_name": provider_name,
            "case": case,
            "arm": arm,
            "sample": sample,
            "turn": turn,
            "prompt_chars": len(prompt or ""),
            "prompt": base._clip(prompt, TRACE_PROMPT_CHARS),
        })

    def record_reply(
        self,
        *,
        run_id: str,
        provider: str,
        provider_name: str,
        case: str,
        arm: str,
        sample: int,
        turn: int,
        prompt: str,
        reply: str,
    ) -> None:
        self.record({
            "event": "reply",
            "run_id": run_id,
            "provider": provider,
            "provider_name": provider_name,
            "case": case,
            "arm": arm,
            "sample": sample,
            "turn": turn,
            "prompt_chars": len(prompt or ""),
            "reply_chars": len(reply or ""),
            "prompt": base._clip(prompt, TRACE_PROMPT_CHARS),
            "reply": base._clip(reply, TRACE_REPLY_CHARS),
        })

    def record_case_complete(
        self,
        *,
        run_id: str,
        provider: str,
        case: str,
        arm: str,
        sample: int,
        row: dict[str, Any],
    ) -> None:
        self.record({
            "event": "case_complete",
            "run_id": run_id,
            "provider": provider,
            "case": case,
            "arm": arm,
            "sample": sample,
            "ok": bool(row.get("ok")),
            "stop_reason": str(row.get("stop_reason") or row.get("error") or ""),
            "turns": int(row.get("turns") or 0),
            "score": row.get("score"),
            "seconds": row.get("seconds"),
            "summary": base._clip(row.get("summary_preview") or row.get("error") or "", 1200),
        })

    def record_run_complete(self, *, run_id: str, provider: str, rows: int) -> None:
        self.record({
            "event": "run_complete",
            "run_id": run_id,
            "provider": provider,
            "rows": rows,
        })

    def flush(self) -> None:
        payload = {
            "probe": "source_connector_done_ab_trace",
            "started_at": self.started_at,
            "updated_at": _timestamp(),
            "updated_elapsed_seconds": round(time.monotonic() - self.started, 3),
            "event_count": len(self.events),
            "events": self.events,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(text.encode("utf-8")) > MAX_TRACE_BYTES:
            raise ValueError("source connector done trace exceeded bounded size")
        base._write_json_atomic(self.path, payload)


def _self_test() -> None:
    class DummyProvider:
        name = "dummy"
        location = "fixture://dummy"

        def __init__(self) -> None:
            self.sent: list[str] = []

        def new_chat(self, timeout=None) -> None:
            pass

        def send(self, text: str, timeout=None) -> str:
            self.sent.append(text)
            return json.dumps({"tool": "done", "args": {"answer": "ok"}})

        def close(self) -> None:
            pass

    provider = DummyProvider()
    wrapper = PromptBoundaryProvider(
        provider,
        trace=None,
        run_id="run-test",
        provider_id="deepseek",
        provider_name="DeepSeek",
        case="pubmed",
        arm="boundary",
        sample=1,
    )
    reply = wrapper.send(
        "Your last done.answer did not pass the research quality review.\n"
        "Report quality failed: citation number(s) appear in the report but not in 来源: [3]."
    )
    assert json.loads(reply)["tool"] == "done"
    assert provider.sent
    assert "Opened-but-not-citable" in provider.sent[0]
    assert "quality-review checklist" in provider.sent[0]
    assert wrapper.overlay_hits == 1
    assert wrapper.boundary_overlay_hits == 1
    assert wrapper.quality_overlay_hits == 1

    finalizer_ledger = ResearchLedger()
    finalizer_ledger.record_open_document(
        SourceDocument.html(
            requested_url="https://example.com/a",
            final_url="https://example.com/a",
            title="Example A",
            text="Alpha evidence line.",
        )
    )
    finalizer_ledger.add_evidence_items([
        EvidenceItem(claim="alpha", source_url="https://example.com/a", excerpt="Alpha evidence line.")
    ])
    finalized = finalize_done_answer(
        "## 结论\nAlpha [s1]\n\n## 关键证据\n- Alpha [s1]\n\n## 反证与限制\n未找到强反证\n\n## 来源质量\n- good\n\n## 搜索覆盖\n- search\n\n## 来源\n[s1] Example A - https://example.com/a",
        finalizer_ledger,
        source_ids={"s1": "https://example.com/a"},
    )
    assert finalized.changed
    assert "[1] Alpha" in finalized.text or "[1] Example A" in finalized.text
    assert finalized.source_count == 1

    baseline_provider = DummyProvider()
    baseline_wrapper = PromptBoundaryProvider(
        baseline_provider,
        trace=None,
        run_id="run-test",
        provider_id="deepseek",
        provider_name="DeepSeek",
        case="pubmed",
        arm="baseline",
        sample=1,
    )
    baseline_wrapper.send(
        "Your last done.answer did not pass the research quality review.\n"
        "Report quality failed: citation number(s) appear in the report but not in 来源: [3]."
    )
    assert baseline_provider.sent
    assert "Opened-but-not-citable" not in baseline_provider.sent[0]
    assert "quality-review checklist" not in baseline_provider.sent[0]
    assert baseline_wrapper.overlay_hits == 0
    rows = [
        {"case": "pubmed", "arm": "baseline", "sample": 1, "score": 4, "done_attempts": 2, "quality_retry_count": 1},
        {"case": "pubmed", "arm": "boundary", "sample": 1, "score": 7, "done_attempts": 1, "quality_retry_count": 0, "first_done_passed": True, "eventual_done_passed": True},
        {"case": "pubmed", "arm": "finalizer", "sample": 1, "score": 8, "done_attempts": 1, "quality_retry_count": 0, "first_done_passed": True, "eventual_done_passed": True},
    ]
    summary = summarize(rows)
    assert summary["by_case"]["pubmed"]["baseline_first_pass_rate"] == 0.0
    assert summary["by_case"]["pubmed"]["boundary_first_pass_rate"] == 1.0
    assert summary["by_case"]["pubmed"]["finalizer_eventual_success_rate"] == 1.0
    assert summary["by_arm"]["finalizer"]["count"] == 1
    assert _pending_case_sample_keys(
        cases=(CASES["pubmed"],),
        arms=("baseline", "finalizer"),
        samples=2,
        existing={("pubmed", "baseline", 1)},
    ) == [
        ("pubmed", "finalizer", 1),
        ("pubmed", "baseline", 2),
        ("pubmed", "finalizer", 2),
    ]
    with tempfile.TemporaryDirectory(prefix="codey-source-connector-done-ab-self-") as td:
        output = Path(td) / "payload.json"
        output.write_text(
            json.dumps({"probe": "source_connector_done_ab", "provider": "qwen", "rows": []}),
            encoding="utf-8",
        )
        try:
            _load_or_new_payload(
                output,
                provider_id="deepseek",
                cases=(CASES["pubmed"],),
                arms=("baseline",),
                samples=1,
            )
        except base.OutputProviderMismatch as exc:
            assert exc.expected == "deepseek"
            assert exc.found == "qwen"
        else:
            raise AssertionError("provider mismatch was not rejected")


def main() -> int:
    parser = argparse.ArgumentParser(description="Live A/B for Research done-boundary prompt tuning")
    parser.add_argument("--provider", choices=(*WEB_PROVIDERS, "all"), default="deepseek")
    parser.add_argument("--case", action="append", help="case name or comma list; defaults to pubmed,arxiv")
    parser.add_argument("--arms", default=DEFAULT_ARMS)
    parser.add_argument("--samples", type=int, default=1, help="repeat each case/arm this many times")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--trace-output", type=Path, default=None, help="trace path; default is next to output with a .trace.json suffix")
    parser.add_argument("--max-turns", type=int, default=24)
    parser.add_argument("--send-timeout", type=float, default=120)
    parser.add_argument("--new-chat-timeout", type=float, default=60)
    parser.add_argument("--open-if-missing", action="store_true")
    parser.add_argument("--rerun-failed", action="store_true")
    parser.add_argument("--no-live-trace", action="store_true", help="disable incremental atomic trace writes")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        _self_test()
        print("self-test ok")
        return 0

    selected_cases = tuple(CASES[name] for name in base._parse_names(args.case, CASES, "case"))
    selected_arms = base._parse_names([args.arms], ARMS, "arm")
    providers = WEB_PROVIDERS if args.provider == "all" else (args.provider,)
    run_id = f"source-connector-done-ab-{int(time.time())}"
    for provider_id in providers:
        output = args.output or _default_output(provider_id)
        if args.provider == "all" and args.output is not None:
            output = args.output.with_name(f"{args.output.stem}-{provider_id}{args.output.suffix}")
        trace: LiveTrace | None = None
        if not args.no_live_trace:
            if args.trace_output is not None:
                trace_output = args.trace_output
                if args.provider == "all":
                    trace_output = args.trace_output.with_name(f"{args.trace_output.stem}-{provider_id}{args.trace_output.suffix}")
            else:
                trace_output = _trace_output_path(output)
            trace = LiveTrace(trace_output)
        try:
            run_provider(
                provider_id,
                cases=selected_cases,
                arms=selected_arms,
                samples=max(1, args.samples),
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
        except base.OutputProviderMismatch as exc:
            print(str(exc), file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
