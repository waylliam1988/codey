"""Hermetic AppContext factory for tests.

Bare ``AppContext()`` is a mixed mode: the runtime log gets an ephemeral
directory, but conversation/provider/facts/snapshot/checkpoint/UI stores
fall back to the real ``~/.codey``. Any test that persists through those
stores must use :func:`make_app_state` instead, or it will read and write
the developer's real home directory (and fail when that directory is not
writable).

Convention:

- persistence-touching tests (conversation, approvals cleanup, research
  restores, facts, snapshots, checkpoints, UI state) → ``make_app_state``;
- pure in-memory tests (run registry, approvals, event bus) → bare
  ``AppContext()`` is fine;
- behavior-pinning tests (ghost/ledger stores are ``None`` without an
  explicit home) → MUST keep bare ``AppContext()``.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.app.server import AppContext


def make_app_state(case: unittest.TestCase, *path_parts: str) -> AppContext:
    """Create an AppContext rooted at an isolated temp dir.

    The temp dir lives until the test finishes (registered via
    ``addCleanup``), so state files stay readable for the whole test and
    never touch the real home directory.
    """
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    parts = path_parts or ("state",)
    return AppContext(Path(tmp.name, *parts))


__all__ = ["make_app_state"]
