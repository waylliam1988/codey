"""The only composition point allowed to import a concrete memory backend."""

from __future__ import annotations

import hashlib
from pathlib import Path

from codey.durable.model import EntryDraft
from codey.durable.session import DurableSession
from codey.memory.codec import unique_scopes
from codey.memory.contracts import MemoryCapabilities, MemoryEvent, MemoryHost
from codey.memory.null import null_capabilities
from codey.memory.service import MemoryService, utc_now
from codey.runtime.core.cancellation import DeadlineExceeded, TaskCancelled, check
from codey.storage.managed_outputs import ManagedOutputStore
from codey.workspace.context_admission import request_scope


def _message_source(entry: EntryDraft, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"message:{digest}:{entry.conversation_id}:{entry.entry_id}"


def source_available(durable: DurableSession, ref: str) -> bool:
    if ref.startswith(("request:", "response:")):
        prefix, _, artifact_id = ref.partition(":")
        try:
            artifact = durable.artifact(artifact_id)
        except KeyError:
            return False
        return (artifact.metadata.get("kind") == "provider_" + prefix and artifact.owner_task_id is not None
                and source_available(durable, "run:" + artifact.owner_task_id))
    if ref.startswith("run:"):
        try:
            task = durable.task(ref.removeprefix("run:"))
            identity = task.state.get("input_entry_id")
            return (durable.has_conversation(task.conversation_id)
                    and isinstance(identity, str)
                    and any(entry.entry_id == identity for entry in durable.context(task.conversation_id)))
        except KeyError:
            return False
    if ref.startswith("conversation:"):
        return durable.has_conversation(ref.removeprefix("conversation:"))
    prefix, _, identity = ref.partition(":")
    if prefix == "message":
        _, _, identity = identity.partition(":")
        session_id, separator, entry_id = identity.rpartition(":")
        return bool(separator and durable.has_conversation(session_id)
                    and any(entry.entry_id == entry_id and entry.kind in {"user_message", "assistant_message"}
                            and isinstance(text := entry.payload.get("text"), str)
                            and _message_source(entry, text) == ref for entry in durable.context(session_id)))
    if prefix in {"record", "decision"}:
        session_id, separator, entry_id = identity.rpartition(":")
        expected = "memory_decision" if prefix == "decision" else "memory_proposal"
        return bool(separator and durable.has_conversation(session_id)
                    and any(entry.entry_id == entry_id and entry.kind == expected
                            for entry in durable.context(session_id)))
    if prefix == "management":
        session_id, separator, entry_id = identity.rpartition(":")
        return bool(separator and durable.has_conversation(session_id)
                    and any(entry.entry_id == entry_id and entry.kind == "memory_management"
                            for entry in durable.context(session_id)))
    return False


def build_memory(state_home: Path | None, durable: DurableSession, *, enabled: bool) -> MemoryService:
    capabilities = null_capabilities()
    if enabled and state_home is not None:
        try:
            from codey.ghost.backend import GhostMemory
        except ModuleNotFoundError as exc:
            if exc.name not in {"codey.ghost", "codey.ghost.backend"}:
                raise
        else:
            backend = GhostMemory(MemoryHost(state_home / "memory" / "ghost", utc_now, check))
            capabilities = MemoryCapabilities(backend, backend, backend, backend, backend, backend, backend, backend, backend)
    return MemoryService(capabilities, source_available=lambda ref: source_available(durable, ref))


def observe_terminal(memory: MemoryService, durable: DurableSession, run_id: str,
                     outputs: ManagedOutputStore | None = None) -> bool:
    try:
        task = durable.task(run_id)
    except KeyError:
        return True
    if task.phase not in {"completed", "failed", "cancelled"}:
        return True
    project = str(task.state.get("project") or "")
    entries = durable.context(task.conversation_id)
    user = next((entry for entry in entries if entry.entry_id == task.state.get("input_entry_id")
                 and entry.kind == "user_message"), None)
    answer_id = durable.submission(task.submission_id).answer_entry_id
    assistant = next((entry for entry in entries if entry.entry_id == answer_id
                      and entry.by_task_id == run_id and entry.kind == "assistant_message"), None)
    user_text = user.payload.get("text") if user is not None else None
    answer = assistant.payload.get("text") if assistant is not None else None
    if task.phase == "completed" and (user is None or not isinstance(user_text, str)
                                      or assistant is None or not isinstance(answer, str)):
        return True
    provenance: tuple[str, ...] = ("run:" + run_id, "conversation:" + task.conversation_id)
    if user is not None and isinstance(user_text, str):
        provenance += (_message_source(user, user_text),)
    if assistant is not None and isinstance(answer, str):
        provenance += (_message_source(assistant, answer),)
    try:
        from codey.app.memory_observation import terminal_memory_facts

        terminal_sequence = (assistant.commit_seq if assistant is not None else
            next(point.commit_seq for point in durable.task_timeline(run_id)
                 if point.task.phase in {"completed", "failed", "cancelled"}))
        at = durable.commit_record(terminal_sequence).committed_at
        from codey.app.memory_candidates import delivered_candidates

        candidates, rejections = delivered_candidates(durable, outputs, run_id=run_id,
            user_text=user_text if isinstance(user_text, str) else "", answer=answer if isinstance(answer, str) else "",
            scope=request_scope(project, task.conversation_id), at=at)
        return memory.observe(MemoryEvent(
            event_id=f"task:{run_id}:{answer_id or task.phase}",
            kind="task.completed" if task.phase == "completed" else "task.failed",
            occurred_at=at,
            session_id=task.conversation_id, project=project,
            run_id=run_id, scope=request_scope(project, task.conversation_id),
            payload={"user_text": user_text if isinstance(user_text, str) else "",
                     "assistant_text": answer if isinstance(answer, str) else "",
                     "candidates": candidates, "candidate_rejections": rejections, "commit_sequence": terminal_sequence,
                     **terminal_memory_facts(durable, outputs, run_id)},
            provenance=provenance,
            control_scopes=unique_scopes(("user", request_scope(project, task.conversation_id),
                                           request_scope("", task.conversation_id))),
        ))
    except (TaskCancelled, DeadlineExceeded):
        # The task is already settled. Cancelling an optional projection must
        # not reopen settlement or escape through its terminal notification.
        return False
    except Exception:
        # Receipt inspection is an optional projection after terminal commit.
        # Missing or invalid receipts cannot be relabeled as successful learning.
        import logging

        logging.getLogger(__name__).exception("terminal memory facts are unavailable")
        return False
