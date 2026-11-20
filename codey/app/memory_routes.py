"""Local memory management through canonical Core user commands."""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from codey.app.memory_commands import command_ref, command_time, control_scopes, record_command, selected_scope
from codey.memory.codec import item_from_payload, unique_scopes
from codey.memory.context import retrieve_context
from codey.memory.contracts import (
    MEMORY_CONTROLS,
    DeleteScopeRequest,
    MemoryChange,
    MemoryControl,
    MemoryDecision,
    MemoryEvent,
)
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_boolean, require_json_string
from codey.workspace.context_admission import MEMORY_TOTAL_BUDGET_CHARS, request_scope

if TYPE_CHECKING:
    from codey.app.context import AppContext

_LOG = logging.getLogger(__name__)


def memory_summary(ctx: AppContext, query: dict[str, list[str]]) -> tuple[int, dict[str, object]]:
    project = next(iter(query.get("project", [])), "")
    session_id = next(iter(query.get("session_id", [])), "")
    try:
        selector: JsonObject = {}
        if "scope_kind" in query:
            selector["scope_kind"] = next(iter(query["scope_kind"]), "")
        scope = selected_scope(selector, session_id, project)
        scopes = unique_scopes((scope, "user"))
        policy_scopes = control_scopes(project, session_id) if scope == request_scope("", session_id) else scopes
        pending = [{"proposal_id": proposal.proposal_id, "version": proposal.version,
                    "item": proposal.item.to_payload()} for value in scopes for proposal in ctx.memory.proposals(value)]
        state = ctx.memory.export_state()
        values = state.get("items", [])
        visible: list[JsonValue] = [item for item in values if isinstance(item, dict) and item.get("scope") in scopes] if isinstance(values, list) else []
        records = [{"item": record.item.to_payload(), "state": record.state, "superseded_by": record.superseded_by}
                   for record in ctx.memory.records(scopes)]
        exposure_rows = state.get("exposures", [])
        exposures: list[JsonValue] = [row for row in exposure_rows if isinstance(row, dict) and row.get("scope") in scopes] if isinstance(exposure_rows, list) else []
        return 200, {"ok": True, "enabled": state.get("enabled", False), "items": visible,
                     "pending": pending, "records": records, "selected_scope": scope, "exposures": exposures,
                     "controls": ctx.memory.policy(policy_scopes).to_payload(),
                     "scope": {"session_id": session_id, "project": project}}
    except Exception:
        _LOG.exception("memory summary failed")
        return 503, {"ok": False, "error": "memory unavailable"}


def _propose(ctx: AppContext, payload: JsonObject, session_id: str, project: str, scope: str) -> dict[str, object]:
    fields: JsonObject = {key: payload[key] for key in
                         ("kind", "text", "slot", "context", "supersedes", "expires_at", "confidence") if key in payload}

    def validate(at: str) -> None:
        checked = item_from_payload({**fields, "item_id": "proposal-validation", "scope": scope,
                                      "created_at": at, "source_refs": ["pending-user-command"]})
        if checked.kind == "experience":
            raise ValueError("only preference, goal or hypothesis proposals are reviewable")

    entry = record_command(ctx, session_id, "memory_proposal", {**fields, "scope": scope},
                           require_json_string(payload.get("request_id", ""), "request_id"), validate=validate)
    persisted = ctx.memory.observe(MemoryEvent(
        "proposal:" + entry.entry_id, "memory.proposed", command_time(entry), session_id, project, "", scope,
        fields, (f"record:{session_id}:{entry.entry_id}", "conversation:" + session_id),
        control_scopes(project, session_id),
    ))
    if not persisted:
        raise OSError("proposal projection failed; retry the same request_id")
    return {"ok": True, "proposal_id": "proposal:" + entry.entry_id}


def _decide(ctx: AppContext, payload: JsonObject, session_id: str, scope: str, accepted: bool) -> dict[str, object]:
    with ctx.durable.control_line():
        proposal_id = require_json_string(payload.get("proposal_id"), "proposal_id")
        version = require_json_string(payload.get("version"), "version")
        prior = next((row for identity in ctx.durable.conversation_ids() for row in ctx.durable.entries(identity)
                      if row.kind == "memory_decision" and row.payload.get("proposal_id") == proposal_id
                      and row.payload.get("version") == version), None)
        if prior is not None and prior.payload.get("accepted") != accepted:
            raise ValueError("this proposal already has a different committed decision")
        if prior is None and not any(row.proposal_id == proposal_id and row.version == version
                                     for row in ctx.memory.proposals(scope)):
            raise ValueError("proposal is missing, changed or already decided")
        entry = prior if prior is not None else ctx.durable.append_message(session_id, "memory_decision", {
            "proposal_id": proposal_id, "version": version,
            "accepted": accepted, "occurred_at": ctx.memory.clock(),
        })
        decided_at = require_json_string(entry.payload.get("occurred_at"), "occurred_at")
        ctx.memory.decide(MemoryDecision(proposal_id, version, accepted,
                                        f"decision:{entry.conversation_id}:{entry.entry_id}", decided_at))
        return {"ok": True}


def _manage(ctx: AppContext, payload: JsonObject, session_id: str, scope: str) -> dict[str, object]:
    action = payload.get("action")
    entry = record_command(ctx, session_id, "memory_management", payload,
                           require_json_string(payload.get("request_id", ""), "request_id"))
    ref, at = command_ref(entry), command_time(entry)
    if action == "control":
        name = require_json_string(payload.get("control"), "control")
        control = next((value for value in MEMORY_CONTROLS if value == name), None)
        if control is None:
            raise ValueError("unknown memory control")
        ctx.memory.set_control(MemoryControl(scope, control, require_json_boolean(payload.get("enabled"), "enabled"), ref, at))
    elif action in {"revoke", "quarantine", "restore"}:
        states: dict[str, MemoryChange] = {
            "revoke": MemoryChange(require_json_string(payload.get("item_id"), "item_id"), scope, "revoked", ref, at),
            "quarantine": MemoryChange(require_json_string(payload.get("item_id"), "item_id"), scope, "quarantined", ref, at),
            "restore": MemoryChange(require_json_string(payload.get("item_id"), "item_id"), scope, "active", ref, at),
        }
        ctx.memory.change(states[require_json_string(action, "action")])
    else:
        from codey.app.memory_learning import learning_action

        return learning_action(ctx, payload, scope, ref, at)
    return {"ok": True}


def memory_action(ctx: AppContext, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    try:
        payload = decode_json_object(body)
        session_id = require_json_string(payload.get("session_id", ""), "session_id")
        project = require_json_string(payload.get("project", ""), "project")
        scope = selected_scope(payload, session_id, project)
        action = payload.get("action")
        if action in {"delete_scope", "reset"}:
            if not session_id or not ctx.durable.has_conversation(session_id):
                raise ValueError("an existing session_id is required")
            entry = record_command(ctx, session_id, "memory_management", payload,
                                   require_json_string(payload.get("request_id", ""), "request_id"))
            result = ctx.memory.delete_scope(DeleteScopeRequest(scope, requested_at=command_time(entry)))
            return 200, {"ok": True, "deleted": result.deleted}
        if action == "rebuild":
            rebuilt = ctx.memory.rebuild()
            return 200, {"ok": True, "events": rebuilt.events, "digest": rebuilt.digest}
        if action == "explain":
            view = retrieve_context(ctx.memory, task=require_json_string(payload.get("query", ""), "query"),
                                    project=project, session_id=session_id,
                                    context=require_json_string(payload.get("context", ""), "context"))
            return 200, {"ok": True, "budget_chars": MEMORY_TOTAL_BUDGET_CHARS, "used_chars": len(view.text),
                         "items": [item.to_payload() for item in view.items],
                         "trace": [trace.to_payload() for trace in view.traces]}
        if action == "explain_run":
            from codey.app.memory_observation import routing_for_run

            run_id = require_json_string(payload.get("run_id"), "run_id")
            if ctx.durable.task(run_id).conversation_id != session_id or ctx.managed_outputs is None:
                raise ValueError("the run must belong to the selected conversation")
            return 200, {"ok": True, "requests": routing_for_run(ctx.durable, ctx.managed_outputs, run_id)}
        if not session_id or not ctx.durable.has_conversation(session_id):
            raise ValueError("an existing session_id is required")
        if action == "propose":
            return 200, _propose(ctx, payload, session_id, project, scope)
        if action in {"accept", "reject"}:
            return 200, _decide(ctx, payload, session_id, scope, action == "accept")
        if action in {"control", "revoke", "quarantine", "restore", "feedback", "predict", "review"}:
            return 200, _manage(ctx, payload, session_id, scope)
        raise ValueError("unsupported memory action")
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    except Exception as exc:
        _LOG.exception("memory management failed")
        ref = hashlib.sha256(str(exc).encode()).hexdigest()[:16]
        return 503, {"ok": False, "error": "memory management failed", "error_ref": ref}
