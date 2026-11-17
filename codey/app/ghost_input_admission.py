"""Bind Ghost deletion identities before canonical task admission is visible."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from codey.durable.input_receipts import InputReceipt
from codey.durable.model import TaskCheckpoint
from codey.ghost.observations import GhostObservationStore
from codey.ghost.sqlite import GhostSchemaVersionError

logger = logging.getLogger(__name__)


def register_ghost_input(
    state_home: Path, task: TaskCheckpoint, *, database_name: str = "durable.sqlite3",
) -> InputReceipt:
    """Write only an identity/scope receipt, never a task or memory projection.

    The application owns both locations: durable.sqlite3 and ghost.sqlite3
    share its runtime directory. Ephemeral applications keep these receipts
    in their temporary directory, without enabling memory or background work.
    A receipt can outlive a failed canonical admission. It contains no
    content and cannot dispatch or learn anything on its own.
    """
    try:
        project = task.state.get("project")
        if project is not None and not isinstance(project, str):
            raise ValueError("Ghost input project must be a string or null")
        store = GhostObservationStore(state_home)
        store.database.bind_input_authority(database_name)
        accepted = store.register_input(
            run_id=task.task_id, session_id=task.conversation_id, project=project or "",
        )
    except (OSError, sqlite3.Error, GhostSchemaVersionError, TypeError, ValueError) as exc:
        # The observation store attempts persistent quarantine when receipt
        # insertion fails. Ghost failure must not cancel the canonical input.
        logger.warning("Ghost intake registration failed: %s", type(exc).__name__)
        return InputReceipt("ghost", False)
    return InputReceipt("ghost", accepted)
