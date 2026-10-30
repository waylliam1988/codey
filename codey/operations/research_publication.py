"""Freeze and settle ordinary Research publication through its original effects."""
from __future__ import annotations

import hashlib
from typing import Any

from codey.durable.model import thaw_json
from codey.knowledge.model import KnowledgeMutation, KnowledgeObject, canonical_json
from codey.knowledge.note import KnowledgeNote
from codey.operations.kernel_receipts import restore_result_receipt, result_receipt_fields
from codey.providers.response_record import json_value
from codey.research.pipeline import ResearchContinuationError
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.effects.effect_records import (
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
    RuntimeEffectStore,
    compute_args_digest,
)
from codey.runtime.write.research_publication import PUBLICATION_TOOL


def publish_synthesis(history: Any, tools: Any, task: str, summary: str, **kwargs: Any) -> str:
    from codey.operations.research_iteration import _persist_synthesis

    policy = kwargs["policy"]
    if not policy.allows("knowledge.write"):
        return ""
    # The request binds all caller inputs, while the command additionally freezes
    # derived note metadata, exact versions and the original knowledge mutations.
    request = json_value({"task": task, "summary": summary,
        **{key: value for key, value in kwargs.items() if key not in {"on_event", "policy"}},
        "policy": policy.to_payload()})
    digest = compute_args_digest(request)
    projection = kwargs.get("projection", "initial")
    identity = "publication-" + hashlib.sha256((history.run_id + ":" + projection).encode()).hexdigest()
    previous = next((row for row in RuntimeEffectStore(durable=history.durable).load_effects(
        history.session_id, history.run_id) if row.intent.effect_id == identity), None)
    if previous is not None:
        envelope = _envelope(history, previous)
        if envelope["request_digest"] != digest:
            raise ResearchContinuationError("research publication differs from its frozen inputs")
        if previous.settlement is not None:
            receipt = restore_result_receipt(previous, store=history.outputs,
                                            session_id=history.session_id, run_id=history.run_id)
            if not receipt.ok or receipt.call.args != envelope:
                raise ResearchContinuationError("research publication has no successful original receipt")
            return _project(tools, str(receipt.canonical["synthesis_id"]))
        return _project(tools, _commit(history, tools.store, identity, envelope, changes=tools.changes))

    def publish(command: dict[str, Any]) -> str:
        state = history.state()
        envelope = {"schema": 1, "request_digest": digest, "result_ref": state.work_ref,
                    "authorization": policy.to_payload(), "command": command}
        ref = history.outputs.write_json_artifact(session_id=history.session_id, run_id=history.run_id, payload=envelope)
        artifact = history.outputs.json_artifact_record(ref, owner_task_id=history.run_id, owner_effect_id=identity,
                                                       metadata={"kind": "research_publication"})
        intent = RuntimeEffectIntent(effect_id=identity, effect_category="tool_call", session_id=history.session_id,
            run_id=history.run_id, phase="research_publication", tool_name=PUBLICATION_TOOL,
            args_digest=compute_args_digest(envelope), display_ref=artifact.handle, replay_class="safe")
        history.mutations.record_research_publication(history.session_id, history.run_id,
                                                     expected=state, intent=intent, artifact=artifact)
        return _project(tools, _commit(history, tools.store, identity, envelope, changes=tools.changes))

    return _persist_synthesis(tools, task, summary, **kwargs, publisher=publish)


def _envelope(history: Any, effect: Any) -> dict[str, Any]:
    artifact = history.durable.artifact(effect.intent.display_ref)
    raw = thaw_json(artifact.metadata.get("ref"))
    if (artifact.owner_task_id != history.run_id or artifact.owner_effect_id != effect.intent.effect_id
            or artifact.metadata.get("kind") != "research_publication" or not isinstance(raw, dict)
            or raw.get("handle") != artifact.handle or raw.get("sha256") != artifact.sha256
            or raw.get("session_id") != history.session_id or raw.get("run_id") != history.run_id):
        raise ResearchContinuationError("publication artifact ownership mismatch")
    envelope = history.outputs.read_json_artifact(raw)
    if compute_args_digest(envelope) != effect.intent.args_digest:
        raise ResearchContinuationError("research publication differs from its frozen inputs")
    return dict(envelope)


def settle_cancelled_publications(history: Any, store: Any) -> bool:
    """Resolve admitted commands before cancellation can replace their result."""
    effects = RuntimeEffectStore(durable=history.durable).load_effects(history.session_id, history.run_id)
    pending = [row for row in effects if row.intent.tool_name == PUBLICATION_TOOL and row.settlement is None]
    for effect in pending:
        _commit(history, store, effect.intent.effect_id, _envelope(history, effect), cancelled=True)
    return bool(pending)


def _commit(history: Any, store: Any, identity: str, envelope: dict[str, Any], *,
            changes: Any = None, cancelled: bool = False) -> str:
    if envelope["result_ref"] != history.state().work_ref:
        raise ResearchContinuationError("pending publication no longer belongs to its original result")
    command = envelope["command"]
    note = KnowledgeNote(**command["note"])
    mutations = [KnowledgeMutation(KnowledgeObject.from_dict(row["value"]), row["expected_revision"])
                 for row in command["mutations"]]
    # Query the original transaction before doing any write. In particular,
    # recovery must never resolve today's knowledge heads or regenerate a note.
    versions = store.repository.committed(command["commit_id"], mutations)
    cancelled = cancelled or bool(history.durable.task(history.run_id).state.get("cancel_requested"))
    if versions is None and cancelled:
        result = ToolResult(ToolCall(PUBLICATION_TOOL, envelope), "Research publication cancelled before commit.",
                            ok=False, canonical={"commit_id": command["commit_id"], "cancelled": True})
        _settle(history, identity, result, status="interrupted", error_code="cancelled")
        return ""
    if versions is None:
        try:
            store.publish_note(note, command["commit_id"], mutations, changes=changes)
        except OSError as exc:
            raise ResearchContinuationError(f"research answer publication failed: {exc}") from exc
        versions = store.repository.committed(command["commit_id"], mutations)
        if versions is None:
            raise ResearchContinuationError("research publication has no committed original versions")
    elif not store.exists(note.id):
        raise ResearchContinuationError("committed research publication lost its original note")
    result = ToolResult(ToolCall(PUBLICATION_TOOL, envelope), canonical_json(versions), ok=True,
                        canonical={"synthesis_id": note.id, "commit_id": command["commit_id"], "versions": versions})
    _settle(history, identity, result)
    return str(note.id)


def _settle(history: Any, identity: str, result: ToolResult, *, status: str = "ok", error_code: str = "") -> None:
    settlement = RuntimeEffectSettlement(effect_id=identity, effect_category="tool_call", session_id=history.session_id,
        run_id=history.run_id, status=status, error_code=error_code, replay_class="safe",
        **result_receipt_fields(result, store=history.outputs, session_id=history.session_id,
                                run_id=history.run_id, effect_id=identity))
    history.mutations.record_research_publication(history.session_id, history.run_id,
                                                 expected=history.state(), settlement=settlement)


def _project(tools: Any, identity: str) -> str:
    if not identity:
        from codey.runtime.core.cancellation import TaskCancelled

        raise TaskCancelled("research publication cancelled before commit")
    tools.synthesis_id = identity
    if identity not in tools.created_ids:
        tools.created_ids.append(identity)
    return str(identity)
