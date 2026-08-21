"""Bounded evidence-only follow-up controller for Research pipeline.

This module restricts follow-up model interaction to a single turn and a single
action: extracting verified fact evidence via ``knowledge_write``. All other
tools (search, open, done, link) and internal source IDs (s1, s2) are strictly
forbidden by program-level enforcement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from codey import cancellation
from codey.research.identity import clip
from codey.research.plan_executor import PlanExecutionResult
from codey.research.protocols import extract_json_objects
from codey.research.query_planner import ResearchPlan
from codey.research.tools import ResearchTools


_SOURCE_ID_FORBIDDEN_RE = re.compile(r"\b(?:s\d+|source_id|result_id|hit_id)\b", re.IGNORECASE)


@dataclass(frozen=True)
class EvidenceFollowupResult:
    ok: bool = False
    written_note_ids: tuple[str, ...] = ()
    new_evidence_count: int = 0
    new_source_urls: tuple[str, ...] = ()
    stop_reason: str = ""
    errors: tuple[str, ...] = ()

    @property
    def has_new_evidence(self) -> bool:
        return self.ok and (self.new_evidence_count > 0 or len(self.written_note_ids) > 0)


def build_evidence_followup_prompt(
    *,
    question: str,
    initial_summary: str,
    plan: ResearchPlan,
    material: PlanExecutionResult,
    max_context_chars: int = 8000,
) -> str:
    fresh_urls = material.fresh_source_urls
    lines = [
        "You are performing a bounded Evidence-Only follow-up for a research report.",
        "Your ONLY task is to extract factual evidence excerpts from the freshly retrieved material below using `knowledge_write`.",
        "",
        "STRICT RULES:",
        "1. ONLY call the tool `knowledge_write` (type='fact' or 'concept').",
        "2. Do NOT attempt to call `done`, `web_search`, `open_url`, or any other tool.",
        "3. Every source in `sources` or `evidence[].source_url` MUST EXACTLY match one of the Allowed Fresh URLs below.",
        "4. NEVER use internal labels like 's1', 's2', or placeholders. Always use the full URL.",
        "5. You MUST provide explicit evidence items: `evidence: [{'source_url': '...', 'excerpt': '...', 'claim': '...', 'stance': 'supports|contradicts|context'}]`.",
        "",
        f"Target Research Question: {clip(question, 300)}",
        f"Initial Report Summary: {clip(initial_summary, 1200)}",
        f"Plan Reference: {plan.plan_ref}",
        "",
        "Allowed Fresh URLs:",
        *[f"- {url}" for url in fresh_urls],
        "",
        "Retrieved Material:",
        *[f"=== MATERIAL {i+1} ===\n{clip(preview, 2000)}" for i, preview in enumerate(material.previews)],
        "",
        "Output your single `knowledge_write` tool call JSON now.",
    ]
    return clip("\n".join(lines), max(2000, int(max_context_chars or 8000)))


class EvidenceFollowupController:
    """Enforces tool allowlist and URL whitelist for evidence-only follow-up."""

    def __init__(
        self,
        tools: ResearchTools,
        allowed_urls: Sequence[str],
    ) -> None:
        self.tools = tools
        self.allowed_urls = set(str(u).strip() for u in allowed_urls if str(u).strip())

    def execute_tool_call(self, name: str, args: dict[str, Any]) -> str:
        tool_name = str(name or "").strip().lower()
        if tool_name != "knowledge_write":
            return f"ERROR: Tool '{tool_name}' is forbidden in evidence-only follow-up mode. ONLY 'knowledge_write' is allowed."
        sources = args.get("sources")
        source_list = [str(s).strip() for s in (sources if isinstance(sources, list) else [sources]) if str(s).strip()]
        if not source_list:
            return "ERROR: knowledge_write requires at least one source URL in 'sources'."
        for s in source_list:
            if _SOURCE_ID_FORBIDDEN_RE.search(s) and not (s.startswith("http://") or s.startswith("https://")):
                return f"ERROR: Invalid source reference '{s}'. Internal IDs like s1/s2 are strictly forbidden; use canonical URLs."
            if s not in self.allowed_urls:
                return f"ERROR: Source URL '{s}' is not in the allowed fresh material whitelist."
        evidence_raw = args.get("evidence")
        if not evidence_raw:
            return "ERROR: knowledge_write in evidence-only mode requires explicit 'evidence' items."
        evidence_items = evidence_raw if isinstance(evidence_raw, list) else [evidence_raw]
        for item in evidence_items:
            if not isinstance(item, dict):
                return "ERROR: Each evidence item must be a JSON object."
            ev_src = str(item.get("source_url") or item.get("source") or "").strip()
            if not ev_src:
                return "ERROR: Evidence item is missing source_url."
            if _SOURCE_ID_FORBIDDEN_RE.search(ev_src) and not (ev_src.startswith("http://") or ev_src.startswith("https://")):
                return f"ERROR: Invalid evidence source_url '{ev_src}'. Internal IDs like s1/s2 are strictly forbidden."
            if ev_src not in self.allowed_urls:
                return f"ERROR: Evidence source_url '{ev_src}' is not in the allowed fresh material whitelist."
            excerpt = str(item.get("excerpt") or item.get("quote") or "").strip()
            if not excerpt:
                return "ERROR: Evidence item requires a non-empty excerpt string."
        return self.tools.knowledge_write(args)


def run_evidence_followup(
    *,
    provider: Any,
    tools: ResearchTools,
    plan: ResearchPlan,
    material: PlanExecutionResult,
    question: str,
    initial_summary: str = "",
    max_context_chars: int = 8000,
    should_stop: Callable[[], bool] | None = None,
) -> EvidenceFollowupResult:
    if should_stop and should_stop():
        return EvidenceFollowupResult(stop_reason="stopped")
    cancellation.check()
    fresh_urls = tuple(material.fresh_source_urls)
    if not fresh_urls:
        return EvidenceFollowupResult(ok=True, stop_reason="no_fresh_urls")
    prompt = build_evidence_followup_prompt(
        question=question,
        initial_summary=initial_summary,
        plan=plan,
        material=material,
        max_context_chars=max_context_chars,
    )
    controller = EvidenceFollowupController(tools, fresh_urls)
    initial_evidence_count = len(getattr(tools.ledger, "evidence_items", ()))
    written_note_ids: list[str] = []
    errors: list[str] = []
    try:
        reply = provider.send(prompt)
    except cancellation.TaskCancelled:
        raise
    except Exception as exc:
        return EvidenceFollowupResult(
            ok=False,
            stop_reason="provider_error",
            errors=(clip(f"provider send error: {exc}", 200),),
        )
    if should_stop and should_stop():
        return EvidenceFollowupResult(stop_reason="stopped")
    cancellation.check()
    tool_calls = extract_json_objects(reply)
    if not tool_calls:
        return EvidenceFollowupResult(
            ok=False,
            stop_reason="no_tool_calls",
            errors=("Model reply contained no structured tool call JSON",),
        )
    for call in tool_calls:
        tname = str(call.get("tool") or call.get("name") or call.get("action") or "").strip().lower()
        if tname and tname != "knowledge_write":
            return EvidenceFollowupResult(
                ok=False,
                stop_reason="invalid_tool_called",
                errors=(f"Forbidden tool '{tname}' was called in evidence-only follow-up",),
            )
    first_call = tool_calls[0]
    name = str(first_call.get("tool") or first_call.get("name") or first_call.get("action") or "knowledge_write").strip()
    args = first_call.get("args") or first_call.get("parameters") or first_call
    if isinstance(args, dict):
        res = controller.execute_tool_call(name, args)
        if str(res).startswith("ERROR:"):
            errors.append(clip(res, 200))
        elif "saved" in str(res) and "note id=" in str(res):
            parts = str(res).split("note id=")
            if len(parts) > 1:
                nid = parts[1].split()[0].strip()
                if nid:
                    written_note_ids.append(nid)
    final_evidence_count = len(getattr(tools.ledger, "evidence_items", ()))
    new_ev_count = max(0, final_evidence_count - initial_evidence_count)
    return EvidenceFollowupResult(
        ok=new_ev_count > 0 or len(written_note_ids) > 0,
        written_note_ids=tuple(written_note_ids),
        new_evidence_count=new_ev_count,
        new_source_urls=fresh_urls,
        stop_reason="written" if (new_ev_count > 0 or written_note_ids) else "no_evidence_extracted",
        errors=tuple(errors[:10]),
    )



__all__ = [
    "EvidenceFollowupController",
    "EvidenceFollowupResult",
    "build_evidence_followup_prompt",
    "run_evidence_followup",
]
