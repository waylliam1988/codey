"""Bind an optional Research action to native details in the current record."""
from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

from codey.automation.browser_detail import observe_details, read_detail
from codey.operations.browser_upload import audit_upload_open, browser_delivered_source, matches_document
from codey.research.answer_contract import whole_research
from codey.research.source_reading import render_source_read
from codey.runtime.core.models import ToolResult
from codey.task.web_intent import source_url_key
from codey.toolchain.constants import PAGE_DETAIL_TOOL
from codey.toolchain.tool_spec import (
    SOURCE_READ_TOOL_NAMES,
    freeze_spec_parameters,
    json_contract_text,
    spec_for_tool,
    validate_args_with_spec,
)
from codey.types.json_value import JsonObject, require_json_integer, require_json_string

if TYPE_CHECKING:
    from codey.automation.browser_session import BrowserSession
    from codey.operations.kernel_protocol import TurnSnapshot
    from codey.operations.task_execution import ExecutionDelegate
    from codey.operations.task_session import TaskSession
    from codey.runtime.core.models import ToolCall
    from codey.toolchain.tool_spec import ToolSpec


def _can_inspect(session: TaskSession) -> bool:
    return (whole_research(session) and session.policy.allows("browser.interact")
            and session.policy.allows("browser.observe"))


def audit_browser_open(result: ToolResult, session: TaskSession | None, browser: BrowserSession | None,
                       url: str) -> ToolResult:
    if result.call.name == "open_url":
        result = audit_upload_open(result, session, browser)
    source = source_url_key(url)
    if (session is None or browser is None or not _can_inspect(session)
            or not browser_delivered_source(result, source)):
        return result
    document = browser.document_identity()
    if document is None or not matches_document(document, source):
        return result
    audit = {**result.audit, "browser_document": document}
    if (binding := browser.resource_binding()) is not None:
        audit["browser_resource"] = binding
    return replace(result, audit=audit)


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
    if (browser is None or not _can_inspect(session) or snapshot.research_scope is None
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
    allowed = None if snapshot.allowed is None else tuple(dict.fromkeys((*snapshot.allowed, PAGE_DETAIL_TOOL)))
    narrowed = replace(snapshot, allowed=allowed, tool_names=(*snapshot.tool_names, PAGE_DETAIL_TOOL),
        frozen_specs=(*snapshot.frozen_specs[:index], spec, *snapshot.frozen_specs[index:]),
        custom_executors=(*snapshot.custom_executors[:index], (PAGE_DETAIL_TOOL, None), *snapshot.custom_executors[index:]),
        contract_text=json_contract_text((*snapshot.model_specs, spec)))
    lines = ["If the evidence already answers the question, submit it. To check missing details, choose an action below. "
             "These are observed native disclosures in the requested record."]
    for choice in choices:
        control = controls[choice]
        action = {"tool": PAGE_DETAIL_TOOL, "args": {"choice": choice}}
        lines.append(f"- {json.dumps(control.owner, ensure_ascii=False)} / {json.dumps(control.name, ensure_ascii=False)} "
                     + json.dumps(action, ensure_ascii=False))
    return narrowed, "\n".join(lines)


def selected_detail(session: TaskSession | None, browser: BrowserSession, call: ToolCall,
                    spec: ToolSpec | None) -> tuple[dict[str, object], JsonObject]:
    if session is None or not _can_inspect(session) or spec is None or validate_args_with_spec(spec, call.args):
        raise ValueError("Select a detail action from the current Research request.")
    document = _observed_document(session, browser)
    choice = require_json_string(call.args["choice"], "choice")
    if document is not None and (page := read_detail(browser, document, session.task_text, choice)) is not None:
        return page, document
    raise ValueError("This detail action is stale or no longer readable. Observe the current page before selecting again.")


def read_browser_source(delegate: ExecutionDelegate, browser: BrowserSession, call: ToolCall, *, turn: int,
                        tool_index: int) -> tuple[ToolResult, bool, int | None]:
    if not delegate._reserve_research_acquisition(call.name, turn, tool_index):
        return ToolResult(ok=False, call=call, model_text="ERROR: research acquisition budget exhausted"), False, None
    tools = delegate._observation_tools(source_read=True)
    document = None
    if call.name == PAGE_DETAIL_TOOL:
        specs = delegate._frozen_specs
        page, document = selected_detail(delegate.session, browser, call, specs.get(call.name) if specs is not None else None)
    else:
        page = browser.capture()
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
    return result, ok, code
