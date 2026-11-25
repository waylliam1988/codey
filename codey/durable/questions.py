"""Task-owned decisions committed atomically with wakeup or cancellation."""
from __future__ import annotations

import builtins
import copy
import threading
from dataclasses import replace
from typing import TYPE_CHECKING

from codey.durable.errors import CommitConflict
from codey.durable.model import DurableMutation, EffectRecord, EntryDraft, TaskCheckpoint, TaskStatus, thaw_json
from codey.task.questions import question_args
from codey.utils.object_fields import object_fields, sequence_items

if TYPE_CHECKING:
    from codey.durable.session import DurableSession


class DurableUserQuestionStore:
    """Task-owned question facts projected in the durable checkpoint.

    Questions are control state for one admitted task. Their lifecycle uses
    the task revision CAS, so answer acceptance and restart recovery do not
    depend on a second SQLite database.
    """

    def __init__(self, durable: DurableSession) -> None:
        self.durable = durable
        self.lock = threading.RLock()

    @staticmethod
    def _question(task: TaskCheckpoint) -> dict[str, object] | None:
        value = thaw_json(task.state.get("question"))
        return value if object_fields(value) else None

    def _find(self, identity: str) -> tuple[TaskCheckpoint, dict[str, object]] | None:
        for task in self.durable.tasks():
            question = self._question(task)
            if question is not None and question.get("id") == identity:
                return task, question
        return None

    def get(self, identity: str) -> dict[str, object] | None:
        with self.lock:
            found = self._find(identity)
            return None if found is None else found[1]

    def list(self) -> builtins.list[dict[str, object]]:
        with self.lock:
            values = [question for task in self.durable.tasks() if (question := self._question(task)) is not None]
            return values

    def pending(self, session_id: str) -> dict[str, object] | None:
        with self.lock:
            for task in self.durable.tasks():
                if task.conversation_id != session_id:
                    continue
                question = self._question(task)
                if question is not None and question.get("state") == "pending":
                    return question
            return None

    def request(
        self,
        identity: str,
        session_id: str,
        run_id: str,
        question: object,
        continuation: dict[str, object],
    ) -> dict[str, object]:
        content = question_args(question)
        with self.lock:
            try:
                task = self.durable.task(run_id)
            except KeyError as exc:
                raise ValueError("question requires an admitted durable task") from exc
            if task.conversation_id != session_id:
                raise ValueError("question must belong to its task's conversation")
            old = self._question(task)
            if old is not None and (old["id"] == identity or old["state"] == "pending"):
                if old["id"] != identity or old["session_id"] != session_id or old["run_id"] != run_id:
                    raise ValueError("question identity conflict")
                if any(old[key] != value for key, value in content.items()):
                    raise ValueError("question identity conflict")
                return old
            item: dict[str, object] = {
                "schema_version": 1,
                "id": identity,
                "session_id": session_id,
                "run_id": run_id,
                "state": "pending",
                "revision": 1,
                **content,
                "continuation": copy.deepcopy(continuation),
            }
            self.durable.update_task_state(
                run_id,
                {"question": item},
                expected_revision=task.revision,
            )
            return item

    def cancel(self, identity: str, revision: int) -> dict[str, object]:
        with self.lock:
            # A worker can finish parking while this decision is accepted.
            # Retry its checkpoint CAS, preserving the question revision.
            for _attempt in range(3):
                found = self._find(identity)
                if found is None:
                    raise ValueError("question not found")
                task, item = found
                if item["state"] == "cancelled" and item["revision"] == revision + 1:
                    return item
                if item["state"] != "pending" or item["revision"] != revision:
                    raise ValueError("question is no longer pending")
                updated = {**item, "state": "cancelled", "revision": revision + 1}
                try:
                    self.durable.request_cancel(
                        task.task_id,
                        reason="user question cancelled",
                        state_update={"question": updated},
                        expected_revision=task.revision,
                    )
                    return updated
                except CommitConflict:
                    if _attempt == 2:
                        raise
            raise CommitConflict("question transition lost its durable revision race")

    def answer(self, identity: str, revision: int, answer: str) -> dict[str, object]:
        item = self.get(identity)
        if item is None:
            raise ValueError("question not found")
        if item["state"] == "answered" and item.get("answer") == answer:
            return item
        if item["state"] != "pending":
            raise ValueError("question already answered or cancelled")
        options = item["options"]
        if not isinstance(answer, str) or not 0 < len(answer.strip()) <= 8000 or (
            not item["allow_free_text"] and (not sequence_items(options) or answer not in options)
        ):
            raise ValueError("invalid question answer")
        updated: dict[str, object] = {}

        def accept(task: TaskCheckpoint, _effects: tuple[EffectRecord, ...],
                   conversation_revision: int) -> DurableMutation | None:
            nonlocal updated
            current = self._question(task)
            if current is None or current["id"] != identity:
                raise ValueError("question not found")
            if current["state"] == "answered" and current.get("answer") == answer:
                updated = current
                return None
            if current["state"] != "pending" or current["revision"] != revision:
                raise ValueError("question revision conflict")
            if task.status not in {TaskStatus.RUNNING, TaskStatus.WAITING}:
                raise ValueError("question task is not running or suspended")
            head, _revision = self.durable.head(task.conversation_id)
            # Question identity is stable, so accepting it can create exactly
            # one user entry. The answer, wakeup and checkpoint share a commit.
            entry = EntryDraft(f"answer-{identity}", task.conversation_id, head, "user_message",
                               {"text": answer, "question_id": identity}, by_task_id=task.task_id)
            updated = {**current, "state": "answered", "answer": answer, "revision": revision + 1}
            resume = {
                "kind": "resume_run", "source_run_id": task.task_id,
                "instruction": f"The user answered this question: {current['question']}\nAnswer: {answer}\n"
                    "Continue the original task within its existing authorization. "
                    "Command execution still requires its own approval.",
            }
            state = {**dict(task.state), "question": updated, "resume_input": resume, "resume_entry_id": entry.entry_id}
            status = task.status
            if status == TaskStatus.WAITING:
                status = TaskStatus.PENDING
                state.update(attempt_token="", attempt_owner="", attempt_process_id=0)
            next_task = replace(
                task,
                revision=task.revision + 1,
                state=state,
                status=status,
            )
            return DurableMutation(task.conversation_id, conversation_revision, entries=(entry,), task=next_task,
                                   expected_attempt_token=task.attempt_token)

        self.durable.transact_task(str(item["run_id"]), accept)
        return updated

__all__ = ["DurableUserQuestionStore"]
