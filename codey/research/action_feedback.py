"""Concrete research exits from receipts and the request's declared actions."""

from __future__ import annotations

from typing import Any


def repeat_observation(session: Any, scope: Any, tool_names: tuple[str, ...] | None) -> dict[str, Any] | None:
    results = list(session._memory_results.values())
    if len(results) < 2:
        return None
    latest = results[-1]
    if latest.call.name not in {"knowledge_search", "source_search", "submit_research_answer", "save_finding"}:
        return None
    start = max((index + 1 for index, row in enumerate(results[:-1])
                 if row.ok and row.call.name == "submit_research_answer"), default=0)
    previous = next((row for row in reversed(results[start:-1]) if row.call.name == latest.call.name
                     and row.call.args.get("question_ref", scope.question_ref) == scope.question_ref), None)
    if previous is None or previous.ok != latest.ok or previous.model_text != latest.model_text:
        return None
    if latest.ok and latest.call.name not in {"knowledge_search", "source_search"}:
        return None
    allowed = set(tool_names or ())
    actions = []
    if "read_tool_result" in allowed and any(row.ok and row.canonical.get("read_passages") for row in results):
        actions.append("read_tool_result: recover an indexed original read window")
    actions.extend(name + ": read further original evidence at an observed source/locator"
                   for name in ("open_url", "reopen_source", "open_hit") if name in allowed)
    if "web_search" in allowed:
        actions.append("web_search: seek a new source lead for the missing relation, then read it")
    if "knowledge_read" in allowed:
        actions.append("knowledge_read: read an exact observed stored knowledge id, not a source/passage id")
    if "submit_research_answer" in allowed:
        if scope.submission_mode == "inline" and scope.passage_refs:
            actions.append("submit_research_answer: one finding object with the complete supported answer and its passages")
        if scope.finding_refs:
            actions.append("submit_research_answer: select an exact current candidate fN")
        actions.append("submit_research_answer: unresolved with findings=[] only when direct support is insufficient")
    return {"tool": latest.call.name, "observation": (
        "The same search result was returned again; it added no newly read original passage."
        if latest.ok else "The same action was rejected again; repeating the unchanged proposal does not repair it."),
        "available_exits": actions,
        "instruction": "Use the shown original windows, candidate findings and receipt/source locators. "
                       "A repeated search does not prove an answer is absent. Do not abandon available direct evidence "
                       "or save a placeholder. Choose only an action declared in this frozen request."}
