"""Bounded evidence-only rules for Research follow-up (no model loop).

Controller, prompts, and validation helpers shared by the production kernel
path (``codey.operations.evidence_followup``). This module never sends to a
provider; the old direct ``provider.send`` loop was removed.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from codey.research.plan_executor import PlanExecutionResult
from codey.research.query_planner import ResearchPlan
from codey.research.tools import ResearchTools
from codey.utils.refs import clip, coerce_int

_SOURCE_ID_FORBIDDEN_RE = re.compile(r"\b(?:s\d+|source_id|result_id|hit_id)\b", re.IGNORECASE)
_ALLOWED_KNOWLEDGE_WRITE_ARGS = frozenset({"type", "title", "body", "sources", "evidence"})
_DONE_NO_RELEVANT_MATERIAL_RE = re.compile(
    r"("
    r"no\s+(?:new\s+)?(?:relevant\s+)?evidence|"
    r"no\s+(?:relevant\s+)?material|"
    r"not\s+relevant|irrelevant|unrelated|"
    r"unable\s+to\s+extract|"
    r"does\s+not\s+support|"
    r"\u6ca1\u6709(?:\u65b0\u7684|\u76f8\u5173)?\u8bc1\u636e|"
    r"\u65e0(?:\u65b0\u7684|\u76f8\u5173)?\u8bc1\u636e|"
    r"\u4e0d\u76f8\u5173|\u65e0\u5173"
    r")",
    re.IGNORECASE,
)
_MIN_CONTEXT_CHARS = 2000
_DEFAULT_CONTEXT_CHARS = 8000
_MAX_CONTEXT_CHARS = 20000


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
        "Your task has exactly two valid exits:",
        "1. If the freshly retrieved material contains directly relevant evidence, output one `knowledge_write` call.",
        "2. If the freshly retrieved material contains no relevant evidence, output one `done` call explaining that no relevant evidence is present.",
        "",
        "STRICT RULES:",
        "1. ONLY call `knowledge_write` or `done`. Do NOT call `web_search`, `open_url`, or any other tool.",
        "2. `knowledge_write` MUST use explicit `args.type='fact'` and a non-empty `evidence` list.",
        "3. Every source in `sources` or `evidence[].source_url` MUST EXACTLY match one of the Allowed Fresh URLs below.",
        "4. NEVER use internal labels like 's1', 's2', or placeholders. Always use the full URL.",
        "5. Each evidence item MUST include `source_url`, `excerpt`, `claim`, and `stance` (`supports`, `contradicts`, or `context`).",
        "6. Allowed `knowledge_write.args` keys are ONLY: type, title, body, sources, evidence.",
        "7. Do NOT include `tags`, `relations`, `metadata`, empty `evidence`, or ordinary note-only writes.",
        "",
        "INVALID OUTPUT EXAMPLES:",
        '- {"tool":"knowledge_write","args":{"type":"fact","title":"...","sources":["..."],"tags":[],"relations":[]}}',
        '- {"tool":"knowledge_write","args":{"type":"fact","title":"...","sources":["..."],"evidence":[]}}',
        '- {"tool":"web_search","args":{"query":"..."}}',
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
        'If relevant evidence exists, output exactly: {"tool":"knowledge_write","args":{"type":"fact","title":"...","body":"...","sources":["..."],"evidence":[{"source_url":"...","excerpt":"...","claim":"...","stance":"supports"}]}}',
        'If no relevant evidence exists, output exactly: {"tool":"done","args":{"summary":"No relevant evidence is present in the fresh material."}}',
    ]
    return clip("\n".join(lines), _context_char_limit(max_context_chars))


def build_evidence_followup_repair_prompt(
    *,
    question: str,
    plan: ResearchPlan,
    material: PlanExecutionResult,
    validation_error: str,
    max_context_chars: int = 8000,
) -> str:
    fresh_urls = material.fresh_source_urls
    lines = [
        "Your previous Evidence-Only follow-up tool call was rejected by the program-level validator.",
        f"Validation error: {clip(validation_error, 600)}",
        "",
        "Repair exactly once using one valid JSON object.",
        "",
        "VALID EXITS:",
        '1. If relevant evidence exists, output exactly one `knowledge_write` call with non-empty `evidence`.',
        '2. If no relevant evidence exists, output exactly one `done` call with a short no-relevant-evidence answer.',
        "",
        "REPAIR RULES:",
        "1. `knowledge_write.args` keys are ONLY: type, title, body, sources, evidence.",
        "2. Do NOT include tags, relations, metadata, source_id, result_id, hit_id, s1, or s2.",
        "3. Do NOT output empty evidence.",
        "4. Every `sources[]` and `evidence[].source_url` value MUST EXACTLY match one Allowed Fresh URL below.",
        "5. Each evidence item MUST include source_url, excerpt, claim, and stance.",
        "",
        f"Target Research Question: {clip(question, 300)}",
        f"Plan Reference: {plan.plan_ref}",
        "",
        "Allowed Fresh URLs:",
        *[f"- {url}" for url in fresh_urls],
        "",
        "Retrieved Material:",
        *[f"=== MATERIAL {i+1} ===\n{clip(preview, 2000)}" for i, preview in enumerate(material.previews)],
        "",
        'Valid `knowledge_write` shape: {"tool":"knowledge_write","args":{"type":"fact","title":"...","body":"...","sources":["..."],"evidence":[{"source_url":"...","excerpt":"...","claim":"...","stance":"supports"}]}}',
        'Valid no-evidence shape: {"tool":"done","args":{"summary":"No relevant evidence is present in the fresh material."}}',
    ]
    return clip("\n".join(lines), _context_char_limit(max_context_chars))


def _reject_forbidden_tool(tool_name: str) -> str | None:
    if tool_name != "knowledge_write":
        return f"ERROR: Tool '{tool_name}' is forbidden in evidence-only follow-up mode. ONLY 'knowledge_write' is allowed."
    return None


def _reject_extra_write_args(args: dict[str, Any]) -> str | None:
    extra_keys = sorted(str(key) for key in args if str(key) not in _ALLOWED_KNOWLEDGE_WRITE_ARGS)
    if extra_keys:
        return "ERROR: Evidence-only follow-up accepts only type/title/body/sources/evidence args; forbidden key(s): " + ", ".join(extra_keys)
    return None


def _reject_bad_note_type(args: dict[str, Any]) -> str | None:
    if "type" not in args or not str(args.get("type") or "").strip():
        return "ERROR: knowledge_write in evidence-only mode requires explicit type='fact'."
    note_type = str(args.get("type") or "").strip().lower()
    if note_type != "fact":
        return f"ERROR: Evidence-only follow-up requires type='fact', got '{note_type}'."
    return None


def _extract_source_list(args: dict[str, Any]) -> tuple[list[str], str | None]:
    sources = args.get("sources")
    if not isinstance(sources, list):
        return ([], "ERROR: evidence-only knowledge_write requires sources to be a non-empty list of URLs.")
    source_list = [str(s).strip() for s in sources if str(s).strip()]
    if not source_list:
        return ([], "ERROR: evidence-only knowledge_write requires sources to be a non-empty list of URLs.")
    return (source_list, None)


def _check_single_source(s: str, allowed_urls: set[str]) -> str | None:
    if _SOURCE_ID_FORBIDDEN_RE.search(s) and not (s.startswith("http://") or s.startswith("https://")):
        return f"ERROR: Invalid source reference '{s}'. Internal IDs like s1/s2 are strictly forbidden; use canonical URLs."
    if s not in allowed_urls:
        return f"ERROR: Source URL '{s}' is not in the allowed fresh material whitelist."
    return None


def _check_source_allowlist(source_list: list[str], allowed_urls: set[str]) -> str | None:
    for s in source_list:
        if error := _check_single_source(s, allowed_urls):
            return error
    return None


def _extract_evidence_items(args: dict[str, Any]) -> tuple[list[Any], str | None]:
    evidence_raw = args.get("evidence")
    if not evidence_raw:
        return ([], "ERROR: evidence-only knowledge_write requires evidence to be a non-empty list.")
    if not isinstance(evidence_raw, list) or not evidence_raw:
        return ([], "ERROR: evidence-only knowledge_write requires evidence to be a non-empty list.")
    return (evidence_raw, None)


def _check_single_evidence_item(item: Any, allowed_urls: set[str], source_list: list[str]) -> str | None:
    if not isinstance(item, dict):
        return "ERROR: Each evidence item must be a JSON object."
    if "source_url" not in item:
        if "source" in item:
            return "ERROR: evidence item requires explicit source_url; 'source' alias is not accepted."
        return "ERROR: evidence item requires explicit source_url."
    ev_src = str(item.get("source_url") or "").strip()
    if not ev_src:
        return "ERROR: evidence item requires explicit source_url."
    if _SOURCE_ID_FORBIDDEN_RE.search(ev_src) and not (ev_src.startswith("http://") or ev_src.startswith("https://")):
        return f"ERROR: Invalid evidence source_url '{ev_src}'. Internal IDs like s1/s2 are strictly forbidden."
    if ev_src not in allowed_urls:
        return f"ERROR: Evidence source_url '{ev_src}' is not in the allowed fresh material whitelist."
    if ev_src not in source_list:
        return f"ERROR: Evidence source_url '{ev_src}' must be declared in the note's 'sources' list."
    excerpt = str(item.get("excerpt") or item.get("quote") or "").strip()
    if not excerpt:
        return "ERROR: Evidence item requires a non-empty excerpt string."
    return None


def _check_evidence_allowlist(
    evidence_items: list[Any], allowed_urls: set[str], source_list: list[str]
) -> str | None:
    for item in evidence_items:
        if error := _check_single_evidence_item(item, allowed_urls, source_list):
            return error
    return None


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
        if error := _reject_forbidden_tool(tool_name):
            return error
        if error := _reject_extra_write_args(args):
            return error
        if error := _reject_bad_note_type(args):
            return error
        source_list, error = _extract_source_list(args)
        if error:
            return error
        if error := _check_source_allowlist(source_list, self.allowed_urls):
            return error
        evidence_items, error = _extract_evidence_items(args)
        if error:
            return error
        if error := _check_evidence_allowlist(evidence_items, self.allowed_urls, source_list):
            return error
        return self.tools.knowledge_write(args)


def _done_reports_no_relevant_material(args: object) -> bool:
    if not isinstance(args, dict):
        return False
    text = " ".join(
        str(args.get(key) or "")
        for key in ("summary",)
        if args.get(key) is not None
    )
    return bool(text and _DONE_NO_RELEVANT_MATERIAL_RE.search(text))


def _context_char_limit(value: object) -> int:
    try:
        parsed = coerce_int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = _DEFAULT_CONTEXT_CHARS
    return max(_MIN_CONTEXT_CHARS, min(_MAX_CONTEXT_CHARS, parsed))


__all__ = [
    "EvidenceFollowupController",
    "EvidenceFollowupResult",
    "build_evidence_followup_prompt",
    "build_evidence_followup_repair_prompt",
    ]
