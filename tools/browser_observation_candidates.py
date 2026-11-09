"""Experimental presentation from settled receipts, never another browser state store."""
from __future__ import annotations

import difflib
import json
import re
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from codey.operations.task_session import TaskSession
from codey.research.answer_contract import AnswerScope
from codey.research.ledger import ResearchLedger
from codey.research.tool_output import ResearchToolOutput
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject

PROJECTIONS = ("compact", "delta", "bounded_read", "settle_8s")


def _snapshot(result: ToolResult) -> JsonObject | None:
    observation = result.canonical.get("browser_observation")
    if result.ok and result.call.name == "browser_snapshot" and isinstance(observation, dict):
        return observation
    return None


def _reference_scope(observation: JsonObject) -> str | None:
    elements = observation.get("elements")
    if not isinstance(elements, list) or not elements:
        return None
    scopes: set[str] = set()
    for element in elements:
        ref = element.get("ref") if isinstance(element, dict) else None
        match = re.fullmatch(r"b([0-9a-f]{32})g[1-9][0-9]*e[1-9][0-9]*", ref) if isinstance(ref, str) else None
        if match is None:
            return None
        scopes.add(match[1])
    return next(iter(scopes)) if len(scopes) == 1 else None


def present_snapshot(session: TaskSession, result: ToolResult, *, delta: bool) -> str:
    current = _snapshot(result)
    if current is None:
        raise ValueError("snapshot presentation requires an actual successful snapshot receipt")
    text = "Browser observation only; not citable source evidence. Old refs are invalid.\n"
    current_json = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
    if not delta or current.get("complete") is not True:
        return text + "Full current observation:\n" + current_json
    prior: tuple[str, JsonObject] | None = None
    for identity, row in session._memory_results.items():
        if row is result:
            break
        if (observation := _snapshot(row)) is not None:
            prior = identity, observation
    scope = _reference_scope(current)
    if (prior is None or prior[1].get("page_url") != current.get("page_url")
            or prior[1].get("complete") is not True or scope is None or _reference_scope(prior[1]) != scope):
        return text + "Delta baseline unavailable; full current observation:\n" + current_json
    previous_text, current_text = str(prior[1].get("text") or ""), str(current.get("text") or "")
    diff = "\n".join(difflib.unified_diff(previous_text.splitlines(), current_text.splitlines(),
                                        fromfile="baseline", tofile="current", lineterm=""))
    metadata = {key: value for key, value in current.items() if key != "text"}
    return (text + f"Baseline from settled receipt {prior[0]} (text only, no usable old refs):\n"
            + json.dumps(previous_text, ensure_ascii=False) + "\nCurrent text changes:\n" + (diff or "No text changes.")
            + "\nCurrent metadata and fresh element refs:\n" + json.dumps(metadata, ensure_ascii=False))


@contextmanager
def observation_candidate(name: str) -> Iterator[None]:
    """Patch presentation/defaults only inside the existing experiment process."""
    from codey.operations.task_guidance import task_result_context

    def result_context(session: TaskSession, result: ToolResult, scope: AnswerScope | None) -> str:
        if _snapshot(result) is not None:
            return present_snapshot(session, result, delta=name == "delta")
        return task_result_context(session, result, scope)

    def bounded_read(session: TaskSession, ledger: ResearchLedger, call: ToolCall,
                     opened: ResearchToolOutput, *, provider: object = None) -> ResearchToolOutput:
        return opened

    with ExitStack() as stack:
        if name in {"compact", "delta"}:
            stack.enter_context(patch("codey.operations.kernel_context.task_result_context", result_context))
        elif name == "bounded_read":
            stack.enter_context(patch("codey.research.answer_context.fitting_full_read", bounded_read))
        elif name == "settle_8s":
            stack.enter_context(patch("codey.research.browser_search._FETCH_SETTLE_TIMEOUT_SECONDS", 8.0))
        elif name:
            raise ValueError("unknown observation candidate: " + name)
        yield
