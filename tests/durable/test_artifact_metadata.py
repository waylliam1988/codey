from dataclasses import replace
from pathlib import Path

import pytest

from codey.durable import (
    ArtifactRecord,
    CommitConflict,
    DurableMutation,
    DurableSession,
    EffectRecord,
    MemoryDurableStore,
    SQLiteDurableStore,
)
from codey.storage.managed_outputs import ManagedOutputStore


def artifact(*, handle: str = "artifact-1", task_id: str | None = "t", effect_id: str | None = None) -> ArtifactRecord:
    return ArtifactRecord(
        handle=handle,
        sha256="a" * 64,
        byte_count=12,
        storage_path=f"managed_outputs/{handle}.txt",
        owner_task_id=task_id,
        owner_effect_id=effect_id,
        created_at="2026-01-01T00:00:00+00:00",
        metadata={"kind": "tool-output"},
    )


def _session(backend: str, tmp_path: Path) -> DurableSession:
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    session = DurableSession(store)
    session.admit_input(conversation_id="c", request_id="r", task_id="t", text="run", provider_snapshot={})
    session.start_task("t", state={})
    return session


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_artifact_metadata_is_durable_and_queryable(backend: str, tmp_path: Path) -> None:
    session = _session(backend, tmp_path)
    try:
        task = session.task("t")
        _head, revision = session.store.head("c")
        session.store.commit(DurableMutation("c", revision, artifacts=(artifact(),)))
        assert session.store.artifact("artifact-1") == artifact()
        assert session.store.artifacts_for_task("t") == (artifact(),)
        assert session.commit_sequence() > 0
        if backend == "sqlite":
            path = tmp_path / "durable.sqlite3"
            reopened = DurableSession(SQLiteDurableStore(path))
            try:
                assert reopened.store.artifact("artifact-1") == artifact()
            finally:
                reopened.close()
        assert session.task("t") == task
    finally:
        session.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_artifact_requires_an_existing_owner(backend: str, tmp_path: Path) -> None:
    session = _session(backend, tmp_path)
    try:
        _head, revision = session.store.head("c")
        with pytest.raises(CommitConflict, match="artifact owner"):
            session.store.commit(
                DurableMutation(
                    "c", revision,
                    artifacts=(artifact(task_id="missing"),),
                )
            )
        with pytest.raises(KeyError):
            session.store.artifact("artifact-1")
    finally:
        session.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_effect_settlement_and_artifact_metadata_share_one_commit(backend: str, tmp_path: Path) -> None:
    session = _session(backend, tmp_path)
    try:
        session.advance_task(task_id="t", phase="running", state={})
        session.advance_task(task_id="t", phase="running", state={})
        effect = EffectRecord("e", "t", "tool_call", "safe", "digest", "pending", {"tool": "read"})
        session.record_effect_intent("t", effect)
        settled = replace(effect, status="ok", settlement={"handle": "artifact-1"})
        result = session.settle_effect("t", settled, artifacts=(artifact(effect_id="e"),))
        assert result.record is not None
        assert session.store.effect("e") == settled
        assert session.store.artifact("artifact-1") == artifact(effect_id="e")
    finally:
        session.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_failed_commit_does_not_leave_artifact_metadata(backend: str, tmp_path: Path) -> None:
    session = _session(backend, tmp_path)
    try:
        _head, revision = session.store.head("c")
        with pytest.raises(CommitConflict, match="artifact owner"):
            session.store.commit(
                DurableMutation(
                    "c", revision,
                    artifacts=(artifact(handle="good"), artifact(handle="bad", task_id="missing")),
                )
            )
        with pytest.raises(KeyError):
            session.store.artifact("good")
    finally:
        session.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_conversation_delete_removes_owned_artifact_metadata(backend: str, tmp_path: Path) -> None:
    session = _session(backend, tmp_path)
    try:
        session.register_artifacts("t", (artifact(),))
        session.delete_conversation("c")
        with pytest.raises(KeyError):
            session.artifact("artifact-1")
    finally:
        session.close()


def test_artifact_without_owner_is_rejected() -> None:
    with pytest.raises(ValueError, match="owner"):
        artifact(task_id=None, effect_id=None)


def test_managed_output_builds_verified_durable_record(tmp_path: Path) -> None:
    outputs = ManagedOutputStore(tmp_path)
    ref = outputs.write_tool_output(
        session_id="s", run_id="r", tool_id="e", permission_profile="coding_writer",
        tool_name="run", display_ref="echo", text="output",
    )
    assert ref is not None
    record = outputs.durable_record(
        ref, session_id="s", run_id="r", owner_task_id="t", owner_effect_id="e",
    )
    assert record.handle == ref.handle
    assert record.sha256 == ref.sha256
    assert record.byte_count == ref.stored_bytes
    assert record.storage_path.endswith(f"{ref.handle}.txt")
