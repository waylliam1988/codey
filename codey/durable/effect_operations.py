from __future__ import annotations

from codey.durable.effects import interrupted_effect
from codey.durable.errors import (
    CommitConflict,
)
from codey.durable.model import (
    ArtifactRecord,
    CommitResult,
    DurableMutation,
    EffectRecord,
    TaskStatus,
)
from codey.durable.operations import SessionOperations, operation_line
from codey.durable.reducer import with_effect_intents, with_effect_settlement


class EffectOperations(SessionOperations):
    """Effect intents, receipts, and artifact ownership share one transaction line."""

    @operation_line
    def register_artifacts(self, task_id: str, artifacts: tuple[ArtifactRecord, ...]) -> CommitResult:
        current = self._kernel.store.task(task_id)
        _head, revision = self._kernel.store.head(current.conversation_id)
        if any(artifact.owner_task_id not in {None, task_id} for artifact in artifacts):
            raise ValueError("registered artifacts must belong to the task or an effect")
        return self._kernel._commit(DurableMutation(current.conversation_id, revision, artifacts=artifacts))

    @operation_line
    def record_effect_intent(self, task_id: str, effect: EffectRecord) -> CommitResult:
        return self.record_effect_intents(task_id, (effect,))

    @operation_line
    def record_effect_intents(
        self, task_id: str, effects: tuple[EffectRecord, ...]
    ) -> CommitResult:
        current = self._kernel.store.task(task_id)
        if current.state.get("cancel_requested"):
            raise CommitConflict("task cancellation barrier is active")
        if current.status != TaskStatus.RUNNING:
            raise CommitConflict("only a running task admits unapproved effects")
        _head, revision = self._kernel.store.head(current.conversation_id)
        next_task = with_effect_intents(current, effects)
        return self._kernel._commit(
            DurableMutation(
                conversation_id=current.conversation_id,
                expected_revision=revision,
                task=next_task,
                effects=effects,
            )
        )

    @operation_line
    def settle_effect(
        self,
        task_id: str,
        effect: EffectRecord,
        *,
        expected_status: str = "pending",
        artifacts: tuple[ArtifactRecord, ...] = (),
    ) -> CommitResult:
        self._kernel._ensure_writable()
        if effect.task_id != task_id:
            raise ValueError("effect must belong to the task")
        if effect.settlement is None or effect.status == "pending":
            raise ValueError("effect settlement requires a terminal outcome")
        current = self._kernel.store.task(task_id)
        _head, revision = self._kernel.store.head(current.conversation_id)
        existing = self._kernel.store.effect(effect.effect_id)
        if existing.settlement is not None:
            if existing != effect:
                raise CommitConflict("effect is already settled with a different outcome")
            return CommitResult(revision, self._kernel.store.commit_sequence())
        next_task = with_effect_settlement(current, effect)
        return self._kernel._commit(
            DurableMutation(
                conversation_id=current.conversation_id,
                expected_revision=revision,
                task=next_task,
                effect=effect,
                expected_effect_status=expected_status,
                artifacts=artifacts,
            )
        )

    @operation_line
    def settle_unknown_effect(self, task_id: str, effect_id: str) -> CommitResult:
        """Record an interrupted external effect without replaying it."""
        current = self._kernel.store.task(task_id)
        existing = self._kernel.store.effect(effect_id)
        return self.settle_effect(
            task_id,
            interrupted_effect(current, existing),
            expected_status=existing.status,
        )
