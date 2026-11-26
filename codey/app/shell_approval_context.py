from __future__ import annotations

import threading
from typing import Protocol

from codey.app.approval_registry import ApprovalRegistry
from codey.app.run_registry import RunRegistry
from codey.app.shell_approval_durable import ApprovalFactDurable
from codey.runtime.effects.shell_approval_records import ShellApprovalRecordStore
from codey.storage.managed_outputs import ManagedOutputStore


class ShellApprovalContext(Protocol):
    """The concrete resources needed by shell approval coordination."""

    lock: threading.Lock
    approvals: ApprovalRegistry
    @property
    def durable(self) -> ApprovalFactDurable: ...

    run_registry: RunRegistry
    managed_outputs: ManagedOutputStore | None
    shell_approval_records: ShellApprovalRecordStore
