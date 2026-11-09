"""Single-variable prompt candidates used only by the live experiment process."""
from __future__ import annotations

QUERY_GUIDANCE_ABLATIONS = {
    "query_only_evidence": (
        "A search hit is only a lead; obtain the original passage before citing a factual answer."
    ),
    "query_no_subject": (
        "Use the source's original terminology and a short alternate-language query when the wording differs. "
        "A search hit is only a lead; obtain the original passage before citing a factual answer."
    ),
    "query_no_terms": (
        "Form each search around the exact subject, requested version, operation and missing condition. "
        "A search hit is only a lead; obtain the original passage before citing a factual answer."
    ),
    "query_no_evidence": (
        "Form each search around the exact subject, requested version, operation and missing condition. "
        "Use the source's original terminology and a short alternate-language query when the wording differs."
    ),
}

GUIDANCE = {
    "browser_workflow": (
        "Browser observations are not source evidence. If a page offers the requested record selection, input, "
        "or an expansion, inspect its actual controls and obtain that record before concluding it is absent. "
        "Copy the complete opaque element ref from the newest snapshot; never guess or shorten refs. "
        "After revealing or changing source content, call browser_read before submitting claims about it. "
        "Use the new capture's eN passages for new facts; an older passage cannot support text absent from it. "
        "A delivered click does not verify the intended record: inspect the resulting setting and visible content. "
        "If delivery is uncertain, observe before deciding another action; do not blindly send it again."
    ),
    "query": (
        "Form each search around the exact subject, requested version, operation and missing condition. "
        "Use the source's original terminology and a short alternate-language query when the wording differs. "
        "A search hit is only a lead; obtain the original passage before citing a factual answer."
    ),
    **QUERY_GUIDANCE_ABLATIONS,
    "followup": (
        "After reading, identify the first requested distinction or exception still unsupported. "
        "Search that specific gap with changed terms, or locate the relevant section in the existing capture. "
        "Do not repeat an unchanged search or reacquire a source just because the answer is incomplete."
    ),
    "ranking": (
        "Choose sources by direct support for the requested subject and version. Prefer the original official "
        "specification or primary record over summaries or higher search position; open conflicting versions "
        "when the question requires comparison. Search snippets do not establish a source's full content."
    ),
    "completion": (
        "Before submitting, compare every requested point with the cited original passage: subject, version, "
        "conditions, exceptions and OR alternatives must match. State a precise unresolved gap in place when "
        "support is unavailable. Further acquisition is useful only if it can resolve a remaining gap."
    ),
}
