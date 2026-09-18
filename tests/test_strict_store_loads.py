"""Corrupt local state is backed up, never silently reset (strict readers)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.ghost.router import _read_json_dict, MAX_ROUTER_STATE_BYTES
from codey.ghost.sleep import GhostSleepStore
from codey.providers import controls as provider_controls
from codey.providers import revival as provider_revival
from codey.repairs import adapter_overrides
from codey.research.evidence_ledger import EvidenceLedgerStore
from codey.runs import work_checkpoint as work_checkpoint_module
from codey.storage.conversation_store import ConversationStore
from codey.storage.ui_state_store import UiStateStore
from codey.workspace.facts import ProjectFactsStore


def _corrupt(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")


class StrictStoreLoadTests(unittest.TestCase):
    def test_conversation_store_backs_up_corrupt_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ConversationStore(td)
            path = store.path_for("session-1")
            _corrupt(path)

            context = store.load("session-1")

            self.assertFalse(context.initialized)
            self.assertTrue(path.with_name(path.name + ".corrupt").exists())

    def test_project_facts_reset_corrupt_file_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ProjectFactsStore(td)
            path = store.path_for("proj")
            _corrupt(path)

            facts = store.load("proj")

            self.assertEqual(facts.commands, ())
            self.assertTrue(path.with_name(path.name + ".corrupt").exists())

    def test_ui_state_store_resets_corrupt_file_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = UiStateStore(td)
            _corrupt(store.path)

            state = store.load()

            self.assertIsInstance(state, dict)
            self.assertTrue(store.path.with_name(store.path.name + ".corrupt").exists())

    def test_work_checkpoint_returns_none_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = work_checkpoint_module.WorkCheckpointStore(td)
            path = store.path_for("session-1")
            _corrupt(path)

            self.assertIsNone(store.load("session-1"))
            self.assertTrue(path.with_name(path.name + ".corrupt").exists())

    def test_evidence_ledger_keeps_rotation_strategy_for_corrupt_payload(self) -> None:
        # Deliberate exception: the evidence ledger stays on the lenient read
        # so append_record can rotate a corrupt active file into an
        # `-unavailable-` archive (that rotation is its backup strategy).
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceLedgerStore(td)
            path = Path(td) / "ledger.json"
            _corrupt(path)

            self.assertIsNone(store._load_payload(path))
            self.assertFalse(path.with_name(path.name + ".corrupt").exists())
            self.assertTrue(path.exists())

    def test_ghost_sleep_resets_corrupt_state_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostSleepStore(td)
            _corrupt(store.state_path)

            self.assertIsNone(store._read_state_payload_unlocked())
            self.assertTrue(
                store.state_path.with_name(store.state_path.name + ".corrupt").exists()
            )

    def test_revival_store_rebuilds_from_backup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "provider.json"
            _corrupt(path)

            self.assertEqual(provider_revival._load_store(path), {})
            self.assertTrue(path.with_name(path.name + ".corrupt").exists())

    def test_controls_load_resets_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            _corrupt(path)

            self.assertEqual(provider_controls.load_controls(path), {})
            self.assertTrue(path.with_name(path.name + ".corrupt").exists())

    def test_router_dict_reader_backs_up_corrupt_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "router.json"
            _corrupt(path)

            self.assertIsNone(_read_json_dict(path, max_bytes=MAX_ROUTER_STATE_BYTES))
            self.assertTrue(path.with_name(path.name + ".corrupt").exists())

    def test_adapter_override_index_resets_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            index = adapter_overrides._load_index("qwen", td)
            # Fresh state builds the default skeleton.
            self.assertEqual(index.get("schema_version"), 1)

            path = adapter_overrides._index_path("qwen", td)
            _corrupt(path)
            index = adapter_overrides._load_index("qwen", td)

            self.assertEqual(index.get("schema_version"), 1)
            self.assertTrue(path.with_name(path.name + ".corrupt").exists())


if __name__ == "__main__":
    unittest.main()
