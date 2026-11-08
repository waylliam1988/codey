"""Runtime writes work through the declared capability surface alone."""

from collections.abc import Callable
from pathlib import Path

import pytest

from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore
from codey.durable.checkpoint import operation_from_checkpoint
from codey.durable.contracts import RuntimeCheckpointPort
from codey.durable.model import (
    ArtifactRecord,
    CommitResult,
    DurableMutation,
    EffectRecord,
    SubmissionPlacement,
    SubmissionRecord,
    TaskCheckpoint,
)
from codey.runtime.core.cancellation import TaskCancelled
from codey.runtime.core.operation_payload import LEAF_CLOSING, LEAF_WRITER_RUNNING
from codey.runtime.effects.effect_records import RuntimeEffectIntent
from codey.runtime.effects.replay_policy import ReplayClass
from codey.runtime.write.mutation_line import RuntimeMutationLine


class CheckpointDelegate:
    """No DurableSession inheritance or access to undeclared session methods."""

    def __init__(self, session: DurableSession) -> None:
        self.session = session
        self.calls: set[str] = set()

    def task(self, task_id: str) -> TaskCheckpoint:
        self.calls.add("task")
        return self.session.task(task_id)

    def head(self, conversation_id: str) -> tuple[str | None, int]:
        self.calls.add("head")
        return self.session.head(conversation_id)

    def artifact(self, handle: str) -> ArtifactRecord:
        self.calls.add("artifact")
        return self.session.artifact(handle)

    def transact_task(
        self,
        task_id: str,
        derive: Callable[[TaskCheckpoint, tuple[EffectRecord, ...], int], DurableMutation | None],
    ) -> CommitResult | None:
        self.calls.add("transact_task")
        return self.session.transact_task(task_id, derive)

    def merge_generation_boundary(
        self,
        mutation: DurableMutation,
        *,
        task_id: str,
        at: str,
        steering_mode: str = "one-at-a-time",
        follow_up_mode: str = "one-at-a-time",
    ) -> tuple[tuple[SubmissionRecord, ...], DurableMutation]:
        self.calls.add("merge_generation_boundary")
        return self.session.merge_generation_boundary(
            mutation, task_id=task_id, at=at,
            steering_mode=steering_mode, follow_up_mode=follow_up_mode,
        )


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_runtime_uses_only_checkpoint_port_capabilities(tmp_path: Path, backend: str) -> None:
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    session = DurableSession(store)
    session.admit_input(conversation_id="s", request_id="r", task_id="r", text="inspect code",
                        provider_snapshot={"provider_id": "local"})
    session.start_task("r", state={})
    delegate = CheckpointDelegate(session)
    port: RuntimeCheckpointPort = delegate
    line = RuntimeMutationLine(durable=port)
    try:
        line.accept_operation(session_id="s", run_id="r", provider_id="local", turn_budget=8,
                              max_repair_rounds=1, task_kind="research")
        state = line.mark_writer_running("s", "r", provider_id="local")
        assert state is not None
        artifact = ArtifactRecord("work", "a" * 64, 1, "artifacts/work", owner_task_id="r")
        line.advance_execution("s", "r", expected=state, leaf=LEAF_CLOSING, artifact=artifact)
        assert session.artifact("work") == artifact

        session.admit_input(conversation_id="s", request_id="steer", task_id="steer", text="new requirement",
                            provider_snapshot={"provider_id": "local"},
                            placement=SubmissionPlacement.QUEUED, mode="steer")
        state = operation_from_checkpoint(session.task("r"), "s")
        assert state is not None
        before = session.commit_sequence()
        placed = line.settle_kernel_boundary("s", "r", expected=state, settlement=None, boundary="final")
        assert tuple(item.request_id for item in placed) == ("steer",)
        assert session.commit_sequence() == before + 1
        reopened = operation_from_checkpoint(session.task("r"), "s")
        assert reopened is not None and reopened.leaf == LEAF_WRITER_RUNNING

        session.request_cancel("r", reason="cancel publication")
        intent = RuntimeEffectIntent(
            "publish", "tool_call", "s", "r", tool_name="_publish_research_answer",
            args_digest="a" * 64, replay_class=ReplayClass.UNSAFE,
        )
        before = session.commit_sequence()
        with pytest.raises(TaskCancelled, match="publication cancelled"):
            line.record_research_publication("s", "r", expected=reopened, intent=intent)
        assert session.commit_sequence() == before
        assert delegate.calls == {"task", "head", "artifact", "transact_task", "merge_generation_boundary"}
    finally:
        session.close()
