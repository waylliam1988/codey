"""Unavailable-source closure binds actual failed acquisition receipts."""
from __future__ import annotations

import json

from codey.operations.task_session import TaskSession
from codey.task.web_intent import explicit_web_intent, source_url_key
from codey.types.json_value import JsonObject, decode_json_object


def source_gap_context(session: TaskSession) -> str:
    """Promote verified failures into the fresh work view, outside source data."""
    if session.task_kind != "chat" or session.policy.strict_research or session.opened_sources:
        return ""
    requested = explicit_web_intent(session.task_text).urls
    if not requested:
        return ""
    gaps: list[JsonObject] = []
    covered: set[str] = set()
    for ref, result in session._memory_results.items():
        failure = result.canonical.get("source_failure")
        if result.ok or not isinstance(failure, dict):
            continue
        url, detail = failure.get("url"), failure.get("detail")
        if not isinstance(url, str) or not isinstance(detail, str):
            continue
        key = source_url_key(url)
        if key in requested and key not in covered:
            covered.add(key)
            gaps.append({"result_ref": ref, "reason": detail})
    if not requested <= covered:
        return ""
    return ("Unavailable requested sources (verified runtime failures, not opened evidence):\n"
            + json.dumps({"source_gaps": gaps}, ensure_ascii=False)
            + "\nThe requested pages were attempted and no page content was acquired. "
              "Finish with done.summary explaining the observed limitation in the requested final format, "
              "and copy source_gaps above into done. This closes as blocked without claiming verified content. "
              "Do not repeat the failing fetch just to satisfy source-open requirements; "
              "retry only when the user's task requires further attempts.")


def validate_source_gaps(session: TaskSession) -> bool:
    raw = session.last_done_args.get("source_gaps")
    if raw is None:
        return False
    if session.task_kind != "chat" or session.policy.strict_research:
        raise ValueError("source_gaps is for ordinary chat; keep the task's Research completion contract")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 16:
        raise ValueError("source_gaps requires 1..16 failed source receipt references")
    failed_urls: set[str] = set()
    selected: set[str] = set()
    for item in raw:
        gap = decode_json_object(item)
        if set(gap) != {"result_ref", "reason"} or not isinstance(gap["reason"], str) or not gap["reason"].strip():
            raise ValueError("source_gaps requires result_ref and a nonempty reason")
        ref = gap["result_ref"]
        result = session._memory_results.get(ref) if isinstance(ref, str) else None
        failure = result.canonical.get("source_failure") if result is not None else None
        if result is None or result.ok or not isinstance(failure, dict) or ref in selected:
            raise ValueError("source_gaps must select distinct failed acquisition receipts from this task")
        url = failure.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("source_gaps receipt lacks its requested URL")
        selected.add(str(ref))
        failed_urls.add(source_url_key(url))
    requested = explicit_web_intent(session.task_text).urls
    if requested and not requested <= failed_urls | {source_url_key(url) for url in session.opened_sources}:
        raise ValueError("source_gaps must account for every explicitly requested URL")
    return True
