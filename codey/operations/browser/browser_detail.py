"""Bind an optional Research action to native details in the current record."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Protocol

from codey.automation.browser_detail import observe_details, read_detail
from codey.automation.browser_document import same_document
from codey.automation.browser_query import QueryObservationError
from codey.automation.browser_receipts import delivery
from codey.operations.browser import browser_followups
from codey.operations.browser.browser_upload import audit_upload_open, browser_delivered_source, matches_document
from codey.research.source_reading import render_source_read
from codey.runtime.core.models import ToolCall, ToolResult
from codey.task.web_intent import source_url_key
from codey.toolchain.constants import PAGE_DETAIL_TOOL
from codey.toolchain.tool_spec import (
    SOURCE_READ_TOOL_NAMES,
    freeze_spec_parameters,
    spec_for_tool,
    validate_args_with_spec,
)
from codey.types.json_value import JsonObject, decode_json_object, require_json_integer, require_json_string

if TYPE_CHECKING:
    from codey.automation.browser_session import BrowserSession
    from codey.operations.completion_context import CompletionContext
    from codey.operations.kernel.turn_contract import TurnSnapshot
    from codey.operations.task_session import TaskSession
    from codey.research.ledger import ResearchLedger
    from codey.research.tool_output import ResearchToolOutput
    from codey.research.tools import ResearchTools
    from codey.toolchain.tool_spec import ToolSpec


class ResearchSourceReader(Protocol):
    @property
    def session(self) -> TaskSession | None: ...
    def _reserve_research_acquisition(self, name: str, turn: int, tool_index: int) -> bool: ...
    def _observation_tools(self, *, source_read: bool = False) -> ResearchTools: ...
    def _opened_result(self, call: ToolCall, opened: ResearchToolOutput, url: str, *, ledger: ResearchLedger,
                       turn: int, tool_index: int) -> tuple[ToolResult, bool, int | None]: ...


def _can_inspect(session: TaskSession) -> bool:
    return (session.policy.allows("browser.interact")
            and session.policy.allows("browser.observe"))


def audit_browser_open(result: ToolResult, session: TaskSession | None, browser: BrowserSession | None,
                       url: str) -> ToolResult:
    if result.call.name == "open_url":
        result = audit_upload_open(result, session, browser)
    source = source_url_key(url)
    if (session is None or browser is None or not _can_inspect(session)
            or not (browser_delivered_source(result, source) or _retained_failure(result, source))):
        return result
    document = browser.document_identity()
    if document is None or not matches_document(document, source):
        return result
    audit = {**result.audit, "browser_document": document}
    if (binding := browser.resource_binding()) is not None:
        audit["browser_resource"] = binding
    bound = replace(result, audit=audit)
    return audit_record_regions(bound, session, browser, document) if result.ok else bound


def _retained_failure(result: ToolResult, source: str) -> bool:
    acquisition = result.canonical.get("source_acquisition")
    attempts = acquisition.get("attempts") if isinstance(acquisition, dict) else None
    if result.ok or not isinstance(attempts, list) or not attempts:
        return False
    raw: object = attempts[-1]
    if not isinstance(raw, dict):
        return False
    last: Mapping[str, object] = raw
    return (last.get("backend") == "browser"
            and last.get("delivery") == "confirmed" and last.get("failure_code") != "policy_denied"
            and isinstance(url := last.get("final_url"), str) and source_url_key(url) == source)


def audit_record_regions(result: ToolResult, session: TaskSession, browser: BrowserSession,
                          document: JsonObject, *, native_context: str = "") -> ToolResult:
    """Bind observed ownership to offsets in this original formal source, without copying values."""
    from codey.policies.tool_requirement import parse_tool_success_check
    from codey.research.source_document import SourceReadScope
    from codey.research.source_observation import source_observation

    labels = tuple({rule.browser_text.context: None for check in session.policy.required_checks
                    if (rule := parse_tool_success_check(check)) is not None and rule.report_label
                    and rule.browser_text is not None})
    if (not result.ok or not labels or result.audit.get("query_region") is not None
            or not same_document(document, browser.document_identity())):
        return result
    _source, text, _pages = source_observation(result.canonical.get("research_observation"))
    observed: list[JsonObject] = ([{"context": {"label": native_context}, "text": text}] if native_context and native_context in labels else
                                  browser.record_regions(labels))
    regions: list[JsonObject] = []
    for row in observed:
        context = decode_json_object(row["context"])
        scope = SourceReadScope.locate(text, require_json_string(row["text"], "record text"))
        if scope.start is not None and scope.end is not None:
            region: JsonObject = {"label": context["label"], "start": scope.start, "end": scope.end, "complete": True}
            targets = row.get("targets")
            bindings: list[JsonObject] = []
            for raw in targets if isinstance(targets, list) else []:
                target = decode_json_object(raw)
                located = SourceReadScope.locate(text, require_json_string(target["text"], "target text"))
                if (located.start is not None and located.end is not None
                        and scope.start <= located.start < located.end <= scope.end):
                    bindings.append({"start": located.start, "end": located.end, "target": target["target"]})
            if bindings:
                region["query_targets"] = list(bindings)
            regions.append(region)
    return (replace(result, audit={**result.audit, "record_regions": regions})
            if regions and same_document(document, browser.document_identity()) else result)


def _observed_document(session: TaskSession, browser: BrowserSession) -> JsonObject | None:
    with session.projection_lock:
        observations = tuple(row.audit.get("browser_document") for row in session._memory_results.values()
                             if row.ok and row.call.name in SOURCE_READ_TOOL_NAMES | {"browser_read"}
                             and row.canonical.get("read_passages"))
    if not any(isinstance(document, dict) for document in observations):
        return None
    document = browser.document_identity()
    return document if document is not None and document in observations else None


def offer_page_details(session: TaskSession, snapshot: TurnSnapshot,
                       browser: BrowserSession | None) -> tuple[TurnSnapshot, str]:
    from codey.operations.kernel.turn_presentation import refresh_snapshot_contract

    if (browser is None or not _can_inspect(session) or snapshot.research.scope is None
            or not {"browser_read", "browser_click"} <= set(snapshot.nested_tool_names)
            or PAGE_DETAIL_TOOL in session.controller_denied or session.model_tool_names is not None):
        return snapshot, ""
    document = _observed_document(session, browser)
    if document is None:
        return snapshot, ""
    with session.projection_lock:
        completed = {str(row.call.args.get("choice")) for row in session._memory_results.values()
                     if row.ok and row.call.name == PAGE_DETAIL_TOOL}
    controls = {control.choice(document): control for control in observe_details(browser, document, session.task_text)}
    choices = [choice for choice in controls if choice not in completed]
    if not choices or (base := spec_for_tool(PAGE_DETAIL_TOOL)) is None:
        return snapshot, ""
    spec = freeze_spec_parameters(replace(base, parameters=(("choice", {"type": "string", "enum": choices}),)))
    index = len(snapshot.tool_names)
    allowed = snapshot.allowed
    if allowed is not None:
        allowed_names: dict[str, None] = dict.fromkeys((*allowed, PAGE_DETAIL_TOOL))
        allowed = tuple(allowed_names)
    narrowed = refresh_snapshot_contract(replace(snapshot, allowed=allowed, tool_names=(*snapshot.tool_names, PAGE_DETAIL_TOOL),
        frozen_specs=(*snapshot.frozen_specs[:index], spec, *snapshot.frozen_specs[index:]),
        custom_executors=(*snapshot.custom_executors[:index], (PAGE_DETAIL_TOOL, None), *snapshot.custom_executors[index:])))
    lines = ["If the evidence already answers the question, submit it. To check missing details, choose an action below. "
             "These are observed native disclosures in the requested record."]
    for choice in choices:
        control = controls[choice]
        action = {"tool": PAGE_DETAIL_TOOL, "args": {"choice": choice}}
        lines.append(f"- {json.dumps(control.owner, ensure_ascii=False)} / {json.dumps(control.name, ensure_ascii=False)} "
                     + json.dumps(action, ensure_ascii=False))
    return narrowed, "\n".join(lines)


def offer_query_controls(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext,
                         browser: BrowserSession | None) -> tuple[TurnSnapshot, str]:
    """Offer optional ordinary Research queries from the same formally read document."""
    from codey.operations.browser.browser_task_handoff import (
        browser_continuation_handoff,
        confirmed_query_handoff,
        query_result_handoff,
    )
    from codey.operations.browser.turn_surface import offer_browser_call

    if (browser is None or not _can_inspect(session) or snapshot.research.scope is None
            or (document := _observed_document(session, browser)) is None
            or not same_document(document, context.browser_document)):
        return snapshot, ""
    for name in ("browser_snapshot", "browser_click", "browser_read", "browser_wait"):
        snapshot = offer_browser_call(snapshot, session, name, research=True)
    snapshot, recovery = browser_continuation_handoff(session, snapshot, context)
    if recovery:
        return snapshot, recovery
    snapshot, candidates = query_result_handoff(session, snapshot, context, browser.generation, task_named=False)
    return snapshot, "\n\n".join(part for part in (confirmed_query_handoff(session, snapshot, context), candidates) if part)


def initial_query_access(session: TaskSession, snapshot: TurnSnapshot,
                         browser: BrowserSession | None, call: ToolCall) -> ToolCall:
    """Read and observe in one existing program, retaining both formal child receipts."""
    if (browser is None or not _can_inspect(session) or snapshot.research.scope is None
            or call.name != "open_url" or "codemode" not in snapshot.tool_names
            or not {"open_url", "browser_snapshot"} <= set(snapshot.nested_tool_names)):
        return call
    # Source rejection must not suppress observation of the retained page.
    return ToolCall("codemode", {"code": "const source = await tools.open_url(inputs); "
                    "await tools.browser_snapshot({}); return unwrap(source);", "inputs": call.args}, call.call_id)


def selected_detail(session: TaskSession | None, browser: BrowserSession, call: ToolCall,
                    spec: ToolSpec | None) -> tuple[dict[str, object], JsonObject]:
    if session is None or not _can_inspect(session) or spec is None or validate_args_with_spec(spec, call.args):
        raise ValueError("Select a detail action from the current Research request.")
    document = _observed_document(session, browser)
    choice = require_json_string(call.args["choice"], "choice")
    if document is not None and (page := read_detail(browser, document, session.task_text, choice)) is not None:
        return page, document
    raise ValueError("This detail action is stale or no longer readable. Observe the current page before selecting again.")


def read_browser_source(delegate: ResearchSourceReader, browser: BrowserSession, call: ToolCall, *, spec: ToolSpec | None, turn: int,
                        tool_index: int) -> tuple[ToolResult, bool, int | None]:
    if not delegate._reserve_research_acquisition(call.name, turn, tool_index):
        return ToolResult(ok=False, call=call, model_text="ERROR: research acquisition budget exhausted"), False, None
    tools = delegate._observation_tools(source_read=True)
    document = None
    query_ref = ""
    modal_result = False
    session = delegate.session
    if call.name == PAGE_DETAIL_TOOL:
        page, document = selected_detail(session, browser, call, spec)
    else:
        pending = browser_followups.pending_click(session._memory_results, browser.document_identity()) if session is not None else None
        if pending is None or session is None:
            page = browser.capture()
        else:
            query_ref, click = pending
            if delivery(click) == "uncertain":
                raise RuntimeError(browser_followups.UNCERTAIN)
            baseline = decode_json_object(click.audit["query_baseline"])
            raw_document = baseline.get("document")
            document = decode_json_object(raw_document) if isinstance(raw_document, dict) else None
            waits = browser_followups.click_waits(session.policy.required_checks, True)
            refs = tuple(session._memory_results)
            subsequent = (session._memory_results[ref] for ref in refs[refs.index(query_ref) + 1:])
            settled = [row for row in subsequent if row.call.name == "browser_wait" and row.ok
                       and same_document(row.audit.get("query_wait_document"), document)]
            if not all(any(row.call.args == wait for row in settled) for wait in waits):
                raise RuntimeError(browser_followups.PENDING + " Complete the declared wait first.")
            from codey.operations.browser.browser_waits import observed_link_outcome

            modal_result = observed_link_outcome(session, query_ref, document)
            if modal_result:
                page = browser.capture()
            else:
                try:
                    page = browser.capture_query_result(baseline, pending=browser_followups.PENDING)
                except QueryObservationError as exc:
                    return ToolResult(ok=False, call=call, model_text=str(exc),
                        canonical={"query_read": {"reason": exc.reason}},
                        audit={"query_click": query_ref, "browser_document": document}), False, None
    url = str(page.get("url") or "")
    opened = render_source_read(
        tools.gateway.capture(url, page), tools.ledger,
        offset=require_json_integer(call.args.get("offset", 0), "offset"),
        limit=require_json_integer(call.args.get("limit", 6000), "limit"),
        coherent=tools.coherent_passages,
    )
    result, ok, code = delegate._opened_result(call, opened, url, ledger=tools.ledger, turn=turn, tool_index=tool_index)
    if ok and document is not None:
        result = replace(result, audit={**result.audit, "browser_document": document})
    if ok and query_ref and (modal_result or page.get("query_confirmed") is True):
        result = replace(result, audit={**result.audit, "query_click": query_ref})
        region = page.get("query_region")
        if isinstance(region, dict):
            result = replace(result, audit={**result.audit, "query_region": decode_json_object(region)})
    if ok and session is not None and (current := browser.document_identity()) is not None:
        native_context = page.get("record_context", "")
        result = audit_record_regions(result, session, browser, current,
            native_context=native_context if isinstance(native_context, str) else "")
    return result, ok, code
