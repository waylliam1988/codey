"""Publish reusable facts from the committed completion and its original receipts."""
from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from codey.app.memory_behaviors import terminal_tool_result
from codey.app.memory_candidates import accepted_done_decision
from codey.durable.model import TaskCheckpoint
from codey.durable.session import DurableSession
from codey.knowledge.archive import stable_identity
from codey.knowledge.model import KnowledgeMutation, KnowledgeObject, KnowledgeReference
from codey.knowledge.recall import KnowledgeAccess
from codey.knowledge.store import KnowledgeStore
from codey.operations.kernel_contracts import DoneDecision
from codey.policies.redaction import looks_prompt_visible_secret
from codey.runtime.core.models import ToolResult
from codey.runtime.effects.effect_records import RuntimeEffectSettlement
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from codey.utils.object_fields import object_fields
from codey.workspace.revision import WorkspaceIdentity

TerminalReceipt = tuple[ToolResult, RuntimeEffectSettlement]
_TOOLS = frozenset({"edit", "write_file", "run", "browser_read"})
_LOG = logging.getLogger(__name__)


def _public_metadata(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 256 and "\n" not in value and not looks_prompt_visible_secret(value)


def _receipt_ref(settlement: RuntimeEffectSettlement) -> JsonObject:
    return {"effect_id": settlement.effect_id, "sha256": settlement.result_payload_sha256,
            "artifact": settlement.result_ref}


def _reference_order(reference: KnowledgeReference) -> tuple[str, int]:
    return reference.target, reference.revision


def _project_facts(decision: DoneDecision, receipts: Sequence[TerminalReceipt]) -> tuple[
    dict[str, JsonObject], dict[tuple[str, str], JsonObject],
]:
    proof = decode_json_object(decision["result"]).get("proof")
    if not isinstance(proof, dict) or proof.get("satisfied") is not True:
        return {}, {}
    final = WorkspaceIdentity.trusted_pair(decision["workspace_revision"], decision["workspace_fingerprint"])
    if not final.trusted:
        return {}, {}
    edits: dict[str, JsonObject] = {}
    checks: dict[tuple[str, str], JsonObject] = {}
    for result, settlement in receipts:
        version = WorkspaceIdentity.from_audit(result.audit)
        if (result.call.name in {"edit", "write_file"} and result.audit.get("changed") is True
                and version.trusted and version.revision <= final.revision):
            path = result.call.args.get("path")
            if isinstance(path, str) and _public_metadata(path):
                edits[path] = {**_receipt_ref(settlement), "path": path, "workspace_revision": version.revision,
                               "workspace_fingerprint": version.fingerprint}
        elif result.call.name == "run" and version == final and settlement.exit_code == 0:
            command, cwd = result.call.args.get("command"), result.call.args.get("path", ".")
            if isinstance(command, str) and isinstance(cwd, str) and _public_metadata(command) and _public_metadata(cwd):
                checks[command, cwd] = {**_receipt_ref(settlement), "command": command, "cwd": cwd}
    return edits, checks


def project_completed_observations(task: TaskCheckpoint, settled_receipts: Sequence[TerminalReceipt], *,
                                   decision: DoneDecision, decision_ref: str,
                                   store: KnowledgeStore | None = None) -> list[KnowledgeMutation]:
    if task.phase != "completed" or task.state.get("stop_reason") != "done":
        return []
    project = task.state.get("project")
    project = str(Path(project).expanduser().resolve()) if isinstance(project, str) and project else ""
    edits, checks = _project_facts(decision, settled_receipts if project else ())
    material: list[str] = []
    provenance: JsonObject = {"run_id": task.task_id, "completion_receipt": decision_ref}
    if edits and checks:
        material.extend(("Changed files: " + ", ".join(sorted(edits)) + ".",
                         "Checks passed on the final workspace version: " + "; ".join(
                             f"{command} (cwd {cwd})" for command, cwd in sorted(checks)) + "."))
        provenance.update(workspace_revision=decision["workspace_revision"],
                          workspace_fingerprint=decision["workspace_fingerprint"],
                          edits=list[JsonValue](edits.values()), checks=list[JsonValue](checks.values()))
    browser: list[JsonValue] = []
    observed_actions: set[str] = set()
    references: set[KnowledgeReference] = set()
    for result, settlement in settled_receipts:
        if result.call.name != "browser_read" or not isinstance(click := result.audit.get("query_click"), str):
            continue
        observation = result.canonical.get("research_observation")
        source = observation.get("source") if isinstance(observation, dict) else None
        version = source.get("text_hash") if isinstance(source, dict) else None
        if not isinstance(version, str) or click in observed_actions:
            continue
        observed_actions.add(click)
        browser.append({**_receipt_ref(settlement), "click_effect": click, "source_version": version})
        if store is not None and isinstance(source, dict) and isinstance(url := source.get("final_url"), str):
            reference = _read_source_reference(store, project, task.conversation_id, url, version)
            if reference is not None:
                references.add(reference)
    if browser:
        material.append(f"Confirmed a same-document result update for {len(browser)} browser action(s) using the bound region.")
        provenance["browser_observations"] = browser
    if not material:
        return []
    identity = stable_identity("observation", task.task_id)
    ordered_references = tuple(sorted(references, key=_reference_order))
    observation = KnowledgeObject(identity, "observation", "\n".join(material), "observed_result",
        title="Verified project and browser observations" if project else "Verified browser observations",
        status="active", scope="project" if project else "session", scope_id=project or task.conversation_id,
        provenance=provenance, references=ordered_references)
    return [KnowledgeMutation(observation, expected_revision=0)]


def _read_source_reference(store: KnowledgeStore, project: str, session_id: str,
                           url: str, content_hash: str) -> KnowledgeReference | None:
    scope, owner = ("project", project) if project else ("session", session_id)
    identity = stable_identity("source", scope, owner, url, content_hash)
    access = KnowledgeAccess(project, session_id, store.owner_id)
    # The archive creates each immutable captured body at revision 1. Later
    # revisions can withdraw or change it and should mark the observation stale.
    version = store.repository.read(identity, 1)
    if version is not None and access.allows(version) and version.value.kind == "source":
        provenance: object = version.value.provenance
        if (object_fields(provenance) and provenance.get("url") == url
                and provenance.get("content_hash") == content_hash):
            return KnowledgeReference(identity, version.revision, "from_source")
    return None


def observe_knowledge(store: KnowledgeStore, durable: DurableSession, outputs: ManagedOutputStore, run_id: str) -> bool:
    try:
        task = durable.task(run_id)
    except KeyError:
        return True
    try:
        if (task.execution_kind == "tool" or task.phase != "completed" or task.state.get("stop_reason") != "done"
                or task.state.get("strict_research") is True):
            return True
        existing = store.repository.read(stable_identity("observation", run_id))
        if existing is not None:
            return True
        decision, ref = accepted_done_decision(durable, outputs, run_id)
        if decision is None:
            return True
        receipts = [row for effect in durable.settled_effects(run_id)
                    if effect.category == "tool_call" and effect.intent.get("tool_name") in _TOOLS
                    and effect.settlement is not None and effect.settlement.get("result_payload_sha256")
                    and effect.settlement.get("status") == "ok"
                    and (row := terminal_tool_result(effect, outputs, session_id=task.conversation_id))[0].ok]
        mutations = project_completed_observations(task, receipts, decision=decision, decision_ref=ref, store=store)
        if mutations:
            project = mutations[0].value.scope_id if mutations[0].value.scope == "project" else ""
            store.commit_knowledge(f"terminal-observation:{run_id}", mutations, project=project, session_id=task.conversation_id)
        return True
    except KeyError:
        _LOG.exception("Terminal knowledge receipt is missing for %s", run_id)
    except Exception:
        _LOG.exception("Terminal knowledge projection failed for %s", run_id)
    return False
