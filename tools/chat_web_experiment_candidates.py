"""Experimental Chat variants; importing this module never changes production."""
from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Literal
from unittest.mock import patch

from codey.operations import completion_gate, task_entry, task_guidance
from codey.operations.completion_gate import GateVerdict
from codey.operations.kernel_progress import KernelProgress
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research import extract
from codey.research.provenance import _urls_in_text

Variant = Literal["baseline", "recovery", "repeat", "reading", "citations", "combined", "table_cells"]
VARIANTS: tuple[Variant, ...] = ("baseline", "recovery", "repeat", "reading", "citations", "combined", "table_cells")

RECOVERY_GUIDANCE = (
    "Search recovery: when results do not address the requested entity or topic, remove unnecessary "
    "qualifiers while preserving the core entity and necessary location. Do not keep appending synonyms "
    "to a failing query. Open a promising identity or official source, then use the facts discovered there "
    "to choose the next focused question. If the entity remains ambiguous, ask instead of assuming."
)
READING_GUIDANCE = (
    "Reading relevance: before making recommendations, open a relevant result and read the passage "
    "that addresses the user's question. A successful open only establishes the displayed window. "
    "If it contains navigation or other topics, locate the relevant section with source_search and open "
    "the hit, or continue using the reported source offset. A heading or locator preview does not "
    "establish the section's findings. Stop once sufficient relevant text has been read; give its actual "
    "URL and state any remaining gap. Do not substitute unrelated visible facts for the requested topic."
)
REPEAT_GUIDANCE = (
    "The web_search query changed, but its returned output is identical to the previous search; "
    "this added no new information. Reduce unnecessary qualifiers to the core entity and necessary "
    "location, open a relevant existing result, or explain the specific remaining blocker. "
    "Do not keep appending words to the query. Identical results alone do not establish relevance."
)
INTERNAL_REFS = re.compile(r"\[\[?[rsh]\d+(?:\s*[,，、]\s*[rsh]?\d+)*\]\]?(?!\()", re.I)


def cross_query_feedback(progress: KernelProgress) -> str:
    if not progress.idle or progress.stopped or len(progress.attempts) < 2:
        return ""
    previous, latest = list(progress.attempts)[-2:]
    if (previous.tool == latest.tool == "web_search" and previous.call_fp != latest.call_fp
            and previous.result_fp == latest.result_fp and not previous.changed and not latest.changed
            and previous.edit_epoch == latest.edit_epoch):
        return REPEAT_GUIDANCE
    return ""


def citation_identity_problem(session: TaskSession, text: str) -> str:
    """Validate identities, leaving mandatory reads/citations to the frozen policy.

    A discovered URL can be returned as a link without pretending its body was
    read. This check deliberately does not certify the claims surrounding it.
    """
    if session.task_kind != "chat" or session.policy.strict_research:
        return ""
    known = set(session.search_results.values()) | set(session.opened_sources)
    if not known:
        return ""
    if INTERNAL_REFS.search(text):
        return ("Final Chat answer contains internal search/source ids. Replace them with actual source URLs "
                "from the recorded results. Open relevant text before making claims beyond the snippets.")
    unknown = set(_urls_in_text(text)) - known - set(session.required_source_urls)
    if unknown:
        return ("Final Chat answer contains URL(s) absent from the acquired results and requested sources: "
                + ", ".join(sorted(unknown)[:3]) + ". Use recorded URLs or acquire the source first.")
    return ""


@contextmanager
def candidate_variant(variant: Variant) -> Iterator[None]:
    """Patch only experiment-process boundaries around the existing kernel."""
    original_guidance = task_guidance.task_guidance_for_policy
    original_feedback = KernelProgress.repeat_feedback
    original_evaluate = completion_gate.evaluate

    def guidance(policy: TaskPolicy, *, task_kind: str) -> str:
        text = original_guidance(policy, task_kind=task_kind)
        if task_kind == "chat" and policy.allows("web.read") and not policy.strict_research:
            additions = []
            if variant in {"recovery", "combined"}:
                additions.append(RECOVERY_GUIDANCE)
            if variant in {"reading", "combined"}:
                additions.append(READING_GUIDANCE)
            text = "\n".join((text, *additions))
        return text

    def feedback(progress: KernelProgress) -> str:
        return original_feedback(progress) or cross_query_feedback(progress)

    def evaluate(session: TaskSession, done_text: object, *, context: object = None) -> GateVerdict:
        verdict = original_evaluate(session, done_text, context=context)
        if not verdict.complete:
            return verdict
        problem = citation_identity_problem(session, str(done_text or ""))
        return GateVerdict(False, problem, None) if problem else verdict

    with ExitStack() as stack:
        if variant == "table_cells":
            stack.enter_context(patch.object(extract, "_BLOCK_TAGS", extract._BLOCK_TAGS | {"td", "th"}))
        if variant in {"recovery", "reading", "combined"}:
            stack.enter_context(patch.object(task_guidance, "task_guidance_for_policy", guidance))
            stack.enter_context(patch.object(task_entry, "task_guidance_for_policy", guidance))
        if variant in {"repeat", "combined"}:
            stack.enter_context(patch.object(KernelProgress, "repeat_feedback", feedback))
        if variant in {"citations", "combined"}:
            stack.enter_context(patch.object(completion_gate, "evaluate", evaluate))
        yield
