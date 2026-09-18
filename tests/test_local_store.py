from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.automation import browser
from codey.storage import local_store
from codey.providers import controls as provider_controls
from codey.storage.ui_state_store import UiStateStore


class LocalStoreTests(unittest.TestCase):
    def test_runtime_paths_share_one_state_home(self) -> None:
        self.assertEqual(browser.DEFAULT_PROFILE, local_store.DEFAULT_STATE_HOME / "edge-profile")
        self.assertEqual(browser.CDP_STATE_FILE, local_store.DEFAULT_STATE_HOME / "cdp-port.json")
        self.assertEqual(
            provider_controls.CONTROL_STORE,
            local_store.DEFAULT_STATE_HOME / "provider-controls.json",
        )
        self.assertEqual(UiStateStore().path, local_store.DEFAULT_STATE_HOME / "ui-state.json")

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

    def test_strict_reader_distinguishes_missing_from_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing.json"
            self.assertIsNone(local_store.read_json_strict(missing))

            corrupt = Path(td) / "corrupt.json"
            corrupt.write_text("not json", encoding="utf-8")
            with self.assertRaises(local_store.StoreCorruption):
                local_store.read_json_strict(corrupt)

            not_dict = Path(td) / "list.json"
            not_dict.write_text("[1,2]", encoding="utf-8")
            with self.assertRaises(local_store.StoreCorruption):
                local_store.read_json_strict(not_dict)

            ok = Path(td) / "ok.json"
            ok.write_text('{"value":1}', encoding="utf-8")
            self.assertEqual(local_store.read_json_strict(ok), {"value": 1})

    def test_corrupt_backup_never_overwrites_previous_backup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_text("first", encoding="utf-8")
            first = local_store.backup_corrupt_file(path)
            assert first is not None
            self.assertEqual(first.name, "state.json.corrupt")

            path.write_text("second", encoding="utf-8")
            second = local_store.backup_corrupt_file(path)
            assert second is not None
            self.assertEqual(second.name, "state.json.corrupt.1")

            self.assertEqual(first.read_text(encoding="utf-8"), "first")
            self.assertEqual(second.read_text(encoding="utf-8"), "second")
            self.assertFalse(path.exists())

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