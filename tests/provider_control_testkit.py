"""Hermetic doubles for web provider driver tests.

Driver tests must never touch the real user state home: control flow
recovery reads ``CONTROL_STORE`` (with a file lock) on every send. This
mixin redirects the store into a temporary directory and resets the
controls task context around each test.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from codey.providers import controls as provider_controls


class IsolatedProviderControlsMixin:
    def setUp(self) -> None:
        super().setUp()
        self._controls_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._controls_tmp.cleanup)
        self.control_store = Path(self._controls_tmp.name) / "provider-controls.json"
        patcher = mock.patch.object(provider_controls, "CONTROL_STORE", self.control_store)
        self.addCleanup(patcher.stop)
        patcher.start()
        provider_controls.end_task_context()
        self.addCleanup(provider_controls.end_task_context)


__all__ = ["IsolatedProviderControlsMixin"]
