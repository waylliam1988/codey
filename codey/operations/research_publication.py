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
        artifact = history.durable.artifact(previous.intent.display_ref)
        raw = thaw_json(artifact.metadata.get("ref"))
        if (artifact.owner_task_id != history.run_id or artifact.owner_effect_id != identity
                or artifact.metadata.get("kind") != "research_publication" or not isinstance(raw, dict)
                or raw.get("handle") != artifact.handle or raw.get("sha256") != artifact.sha256
                or raw.get("session_id") != history.session_id or raw.get("run_id") != history.run_id):
            raise ResearchContinuationError("publication artifact ownership mismatch")
        envelope = history.outputs.read_json_artifact(raw)
        if envelope["request_digest"] != digest or compute_args_digest(envelope) != previous.intent.args_digest:
            raise ResearchContinuationError("research publication differs from its frozen inputs")
        if previous.settlement is not None:
            receipt = restore_result_receipt(previous, store=history.outputs,
                                            session_id=history.session_id, run_id=history.run_id)
            if not receipt.ok or receipt.call.args != envelope:
                raise ResearchContinuationError("research publication has no successful original receipt")
            return _project(tools, str(receipt.canonical["synthesis_id"]))
        return _commit(history, tools, identity, envelope)

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
        return _commit(history, tools, identity, envelope)

    return _persist_synthesis(tools, task, summary, **kwargs, publisher=publish)


def _commit(history: Any, tools: Any, identity: str, envelope: dict[str, Any]) -> str:
    if envelope["result_ref"] != history.state().work_ref:
        raise ResearchContinuationError("pending publication no longer belongs to its original result")
    command = envelope["command"]
    note = KnowledgeNote(**command["note"])
    mutations = [KnowledgeMutation(KnowledgeObject.from_dict(row["value"]), row["expected_revision"])
                 for row in command["mutations"]]
    # Query the original transaction before doing any write. In particular,
    # recovery must never resolve today's knowledge heads or regenerate a note.
    versions = tools.store.repository.committed(command["commit_id"], mutations)
    if versions is None:
        try:
            tools.store.publish_note(note, command["commit_id"], mutations, changes=tools.changes)
        except OSError as exc:
            raise ResearchContinuationError(f"research answer publication failed: {exc}") from exc
        versions = tools.store.repository.committed(command["commit_id"], mutations)
        if versions is None:
            raise ResearchContinuationError("research publication has no committed original versions")
    elif not tools.store.exists(note.id):
        raise ResearchContinuationError("committed research publication lost its original note")
    result = ToolResult(ToolCall(PUBLICATION_TOOL, envelope), canonical_json(versions), ok=True,
                        canonical={"synthesis_id": note.id, "commit_id": command["commit_id"], "versions": versions})
    settlement = RuntimeEffectSettlement(effect_id=identity, effect_category="tool_call", session_id=history.session_id,
        run_id=history.run_id, status="ok", replay_class="safe",
        **result_receipt_fields(result, store=history.outputs, session_id=history.session_id,
                                run_id=history.run_id, effect_id=identity))
    history.mutations.record_research_publication(history.session_id, history.run_id,
                                                 expected=history.state(), settlement=settlement)
    return _project(tools, note.id)


def _project(tools: Any, identity: str) -> str:
    tools.synthesis_id = identity
    if identity not in tools.created_ids:
        tools.created_ids.append(identity)
    return str(identity)
