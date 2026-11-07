from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pytest

from codey.agents.shell_approval import shell_command_payload
from codey.app.approval_registry import ApprovalRegistry
from codey.app.run_registry import RunRegistry
from codey.app.shell_approval_coordinator import ShellApprovalCoordinator
from codey.app.shell_approval_durable import ApprovalFactDurable
from codey.durable import ApprovalStatus, DurableSession, MemoryDurableStore
from codey.runtime.effects.shell_approval_records import ShellApprovalRecordStore
from codey.storage.managed_outputs import ManagedOutputStore


@dataclass
class ApprovalResources:
    """Only the declared approval capabilities, without an application facade."""

    durable: ApprovalFactDurable
    shell_approval_records: ShellApprovalRecordStore
    lock: threading.Lock = field(default_factory=threading.Lock)
    approvals: ApprovalRegistry = field(default_factory=ApprovalRegistry)
    run_registry: RunRegistry = field(default_factory=RunRegistry)
    managed_outputs: ManagedOutputStore | None = None


@pytest.mark.parametrize("action", ["deny", "deny_locked", "expire"])
def test_approval_decisions_need_only_declared_resources(
    tmp_path: Path, action: Literal["deny", "deny_locked", "expire"],
) -> None:
    durable = DurableSession(MemoryDurableStore())
    resources = ApprovalResources(durable, ShellApprovalRecordStore(tmp_path))
    coordinator = ShellApprovalCoordinator(resources)
    try:
        durable.admit_input(
            conversation_id="conversation", request_id="request", task_id="task",
            text="run a command", provider_snapshot={"provider_id": "local"},
        )
        durable.start_task("task", state={})
        durable.advance_task(task_id="task", phase="running", state={})
        coordinator.add_pending("approval", {
            "id": "approval", "session_id": "conversation", "run_id": "task",
            "command": "echo hello", "command_preview": shell_command_payload("echo hello")["command"],
            "command_sha256": shell_command_payload("echo hello")["command_sha256"],
            "command_chars": shell_command_payload("echo hello")["command_chars"],
            "command_truncated": shell_command_payload("echo hello")["command_truncated"],
            "command_ref": "command-artifact", "command_digest": "command-digest", "cwd": ".",
        })

        if action == "deny":
            assert coordinator.claim("approval", claim_execution=False) is not None
            assert durable.approval("approval").status == ApprovalStatus.DENIED
        elif action == "deny_locked":
            with resources.lock:
                assert coordinator.claim_locked("approval", claim_execution=False) is not None
            assert durable.approval("approval").status == ApprovalStatus.DENIED
        else:
            with resources.lock:
                events = coordinator.expire_locked(run_id="task")
            assert len(events) == 1
            assert events[0]["output"] == "Task stopped; command approval expired."
            assert durable.approval("approval").status == ApprovalStatus.CANCELLED
        assert resources.approvals.shell_snapshot() == {}
        assert durable.effects("task") == ()
    finally:
        durable.close()
