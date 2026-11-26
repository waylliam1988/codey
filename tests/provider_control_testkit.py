"""Hermetic doubles for web provider driver tests.

Driver tests must never touch the real user state home: control flow
recovery reads ``CONTROL_STORE`` (with a file lock) on every send. This
mixin redirects the store into a temporary directory and resets the
controls task context around each test.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

from codey.providers import control_recovery, control_store
from codey.providers import controls as provider_controls


@contextmanager
def control_store_at(path: Path) -> Iterator[None]:
    """Isolate the store default at each implementation lookup site."""
    with (
        mock.patch.object(control_store, "CONTROL_STORE", path),
        mock.patch.object(control_recovery, "CONTROL_STORE", path),
    ):
        yield


class IsolatedProviderControlsMixin:
    def setUp(self) -> None:
        super().setUp()
        self._controls_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._controls_tmp.cleanup)
        self.control_store = Path(self._controls_tmp.name) / "provider-controls.json"
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(control_store_at(self.control_store))
        provider_controls.end_task_context()
        self.addCleanup(provider_controls.end_task_context)


__all__ = ["IsolatedProviderControlsMixin", "control_store_at"]
