from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey import browser, local_store, provider_controls


class LocalStoreTests(unittest.TestCase):
    def test_runtime_paths_share_one_state_home(self) -> None:
        self.assertEqual(browser.DEFAULT_PROFILE, local_store.DEFAULT_STATE_HOME / "edge-profile")
        self.assertEqual(browser.CDP_STATE_FILE, local_store.DEFAULT_STATE_HOME / "cdp-port.json")
        self.assertEqual(
            provider_controls.CONTROL_STORE,
            local_store.DEFAULT_STATE_HOME / "provider-controls.json",
        )

    def test_atomic_json_round_trip_replaces_previous_value(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            local_store.write_json_atomic(path, {"value": 1})
            local_store.write_json_atomic(path, {"value": 2})

            self.assertEqual(local_store.read_json(path), {"value": 2})
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_failed_replace_keeps_previous_value_and_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            local_store.write_json_atomic(path, {"value": "old"})

            with mock.patch.object(local_store.os, "replace", side_effect=OSError("busy")):
                with self.assertRaisesRegex(OSError, "busy"):
                    local_store.write_json_atomic(path, {"value": "new"})

            self.assertEqual(local_store.read_json(path), {"value": "old"})
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_invalid_or_oversized_json_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_text("not json", encoding="utf-8")
            self.assertIsNone(local_store.read_json(path))

            with mock.patch.object(local_store, "MAX_JSON_BYTES", 2):
                self.assertIsNone(local_store.read_json(path))

    def test_project_and_session_keys_are_stable_and_opaque(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            first = local_store.project_key(td)
            second = local_store.project_key(Path(td))

        self.assertEqual(first, second)
        self.assertNotIn("temp", first.lower())
        self.assertEqual(local_store.session_key("chat-1"), local_store.session_key("chat-1"))
        self.assertNotEqual(local_store.session_key("chat-1"), local_store.session_key("chat-2"))


if __name__ == "__main__":
    unittest.main()
