"""Approval facts must retain the command and owner admitted by the request."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from codey.app.shell_approval_durable import record_fact
from codey.durable import CommitConflict, DurableSession, SQLiteDurableStore
from codey.runtime.effects.shell_approval_records import ShellApprovalFact, ShellApprovalRecordStore
from codey.storage.managed_outputs import ManagedOutputStore
from tests.support.sqlite_store import TemporarySQLiteStore


@dataclass(frozen=True)
class FactContext:
    durable: DurableSession
    shell_approval_records: ShellApprovalRecordStore


@pytest.fixture(params=("memory", "sqlite"))
def requested_approval(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[tuple[FactContext, ShellApprovalFact]]:
    backend: object = request.param
    assert backend in ("memory", "sqlite")
    store = SQLiteDurableStore(tmp_path / "durable.sqlite3") if backend == "sqlite" else TemporarySQLiteStore()
    with closing(DurableSession(store)) as durable:
        for task_id, session_id in (("owner", "chat"), ("same-session", "chat"), ("other-session", "other")):
            durable.admit_input(conversation_id=session_id, request_id=task_id, task_id=task_id, text="Run command",
                                provider_snapshot={})
            durable.claim_task(task_id)
        artifact = ManagedOutputStore(tmp_path).write_json_artifact(
            session_id="chat", run_id="owner", payload={"command": "echo approved", "cwd": "."},
        )
        fact = ShellApprovalFact("approval", "chat", "owner", "requested", command_ref=artifact.handle,
                                 command_digest=artifact.sha256, cwd=".")
        ctx = FactContext(durable, ShellApprovalRecordStore(tmp_path))
        assert record_fact(ctx, fact)
        yield ctx, fact


@pytest.mark.parametrize("kind", ("requested", "approved"))
@pytest.mark.parametrize("changed", ("task", "session", "command_ref", "command_digest", "cwd"))
def test_conflicting_fact_cannot_target_existing_approval(
    requested_approval: tuple[FactContext, ShellApprovalFact], kind: str, changed: str,
) -> None:
    ctx, requested = requested_approval
    fact = replace(requested, kind=kind)
    if changed == "task":
        fact = replace(fact, source_run_id="same-session")
    elif changed == "session":
        fact = replace(fact, source_run_id="other-session", session_id="other")
    elif changed == "command_ref":
        fact = replace(fact, command_ref=requested.command_ref + "-other")
    elif changed == "command_digest":
        fact = replace(fact, command_digest="different-digest")
    else:
        assert changed == "cwd"
        fact = replace(fact, cwd="other-directory")
    before = ctx.durable.approval("approval")
    history = ctx.durable.commits_after(0)
    audit = ctx.shell_approval_records.path.read_bytes()
    with pytest.raises(CommitConflict):
        record_fact(ctx, fact)
    assert ctx.durable.approval("approval") == before
    assert ctx.durable.commits_after(0) == history
    assert ctx.shell_approval_records.path.read_bytes() == audit


def test_matching_facts_keep_idempotence_and_normal_transition(
    requested_approval: tuple[FactContext, ShellApprovalFact],
) -> None:
    ctx, requested = requested_approval
    before = ctx.durable.approval("approval")
    assert not record_fact(ctx, requested)
    assert ctx.durable.approval("approval") == before
    approved = replace(requested, kind="approved")
    assert record_fact(ctx, approved)
    after = ctx.durable.approval("approval")
    assert after.status == "approved" and after.revision == before.revision + 1
    assert not record_fact(ctx, approved)
    assert ctx.durable.approval("approval") == after
