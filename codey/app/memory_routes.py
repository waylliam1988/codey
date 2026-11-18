"""User-facing memory review uses canonical conversation records for decisions."""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from codey.memory.contracts import DeleteScopeRequest, MemoryDecision, MemoryEvent
from codey.types.json_value import decode_json_object
from codey.workspace.context_admission import request_scope

if TYPE_CHECKING:
    from codey.app.context import AppContext

_LOG = logging.getLogger(__name__)


def memory_summary(ctx: AppContext, query: dict[str, list[str]]) -> tuple[int, dict[str, object]]:
    project = next(iter(query.get("project", [])), "")
    session_id = next(iter(query.get("session_id", [])), "")
    scope = request_scope(project, session_id)
    try:
        proposals = [{"proposal_id": proposal.proposal_id, "version": proposal.version,
                      "item": proposal.item.to_payload()} for proposal in ctx.memory.proposals(scope)]
        state = ctx.memory.export_state()
        items = state.get("items", [])
        visible: list[object] = [item for item in items if isinstance(item, dict) and item.get("scope") in (scope, "user")] if isinstance(items, list) else []
        return 200, {"ok": True, "enabled": state.get("enabled", False), "items": visible,
                     "pending": proposals, "scope": {"session_id": session_id, "project": project}}
    except Exception:
        _LOG.exception("memory summary failed")
        return 503, {"ok": False, "error": "memory unavailable"}


def memory_action(ctx: AppContext, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    try:
        payload = decode_json_object(body)
        action = payload.get("action")
        session_id, project = payload.get("session_id", ""), payload.get("project", "")
        if not isinstance(session_id, str) or not isinstance(project, str):
            raise ValueError("session_id and project must be strings")
        scope = request_scope(project, session_id)
        if action == "delete_scope":
            result = ctx.memory.delete_scope(DeleteScopeRequest(scope))
            return 200, {"ok": True, "deleted": result.deleted}
        if not session_id or not ctx.durable.has_conversation(session_id):
            raise ValueError("an existing session_id is required")
        if action == "propose":
            kind, text = payload.get("kind"), payload.get("text")
            if not isinstance(kind, str) or kind not in {"preference", "goal", "hypothesis"} or not isinstance(text, str) or not text.strip():
                raise ValueError("proposal requires a preference, goal or hypothesis and nonempty text")
            entry = ctx.durable.append_message(session_id, "memory_proposal", payload)
            persisted = ctx.memory.observe(MemoryEvent(
                event_id="proposal:" + entry.entry_id, kind="memory.proposed", occurred_at=ctx.memory.clock(),
                session_id=session_id, project=project, run_id="", scope=scope,
                payload={"kind": kind, "text": text},
                provenance=(f"record:{session_id}:{entry.entry_id}", "conversation:" + session_id),
            ))
            if not persisted:
                return 503, {"ok": False, "error": "proposal projection failed", "proposal_id": "proposal:" + entry.entry_id}
            return 200, {"ok": True, "proposal_id": "proposal:" + entry.entry_id}
        if action in ("accept", "reject"):
            with ctx.durable.control_line():
                proposal_id, version = payload.get("proposal_id"), payload.get("version")
                proposal = next((row for row in ctx.memory.proposals(scope)
                                 if row.proposal_id == proposal_id and row.version == version), None)
                if proposal is None:
                    raise ValueError("proposal is missing, changed or already decided")
                # The durable record owns the user's decision. Ghost receives a
                # reference and projects it; it never acquires an approval capability.
                prior = next((row for row in ctx.durable.context(session_id)
                              if row.kind == "memory_decision"
                              and row.payload.get("proposal_id") == proposal.proposal_id
                              and row.payload.get("version") == proposal.version), None)
                if prior is not None and prior.payload.get("accepted") != (action == "accept"):
                    raise ValueError("this proposal already has a different committed decision")
                entry = prior if prior is not None else ctx.durable.append_message(session_id, "memory_decision", {
                    "proposal_id": proposal.proposal_id, "version": proposal.version,
                    "accepted": action == "accept",
                })
                ctx.memory.decide(MemoryDecision(proposal.proposal_id, proposal.version, action == "accept",
                                                f"decision:{session_id}:{entry.entry_id}", ctx.memory.clock()))
                return 200, {"ok": True}
        raise ValueError("unsupported memory action")
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    except Exception as exc:
        _LOG.exception("memory management failed")
        ref = hashlib.sha256(str(exc).encode()).hexdigest()[:16]
        return 503, {"ok": False, "error": "memory management failed", "error_ref": ref}
