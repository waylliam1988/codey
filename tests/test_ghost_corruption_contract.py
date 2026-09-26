"""Red-first: corrupted observation log must never report success.

Covers review item 1: mid-file corruption -> delete_scope must not return 0/200-ok,
export must not return ok=true with empty observations.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.observations import GhostObservationCorruptedError, GhostObservationStore


def _write_mid_corrupted(store: GhostObservationStore) -> None:
    assert store.append_completed(
        run_id="r1", session_id="s1", mode="chat",
        user_text="hello", assistant_text="hi",
        stop_reason="done", provider_id="local",
    )
    assert store.append_completed(
        run_id="r2", session_id="s1", mode="chat",
        user_text="second", assistant_text="hi2",
        stop_reason="done", provider_id="local",
    )
    lines = store.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    store.path.write_text(lines[0] + "\n" + "{BAD JSON\n" + lines[1] + "\n", encoding="utf-8")
    read = store.log.read()
    assert read.blocked, "fixture must be blocked (mid-file corruption)"


class GhostCorruptionContractTests(unittest.TestCase):
    def test_delete_scope_blocked_raises_not_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostObservationStore(Path(td))
            _write_mid_corrupted(store)
            with self.assertRaises(GhostObservationCorruptedError):
                store.delete_scope("session", session_id="s1")
            # File must be untouched: both records still present as raw lines.
            text = store.path.read_text(encoding="utf-8")
            self.assertIn("r1", text)
            self.assertIn("r2", text)

    def test_export_state_blocked_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostObservationStore(Path(td))
            _write_mid_corrupted(store)
            with self.assertRaises(GhostObservationCorruptedError):
                store.export_state()

    def test_control_delete_scope_blocked_is_500_not_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostObservationStore(Path(td))
            _write_mid_corrupted(store)
            surf = GhostControlSurface.from_state_home(Path(td))
            code, payload = surf.dispatch_action(
                {"action": "delete_scope", "confirm": True, "scope": "session", "session_id": "s1"}
            )
            self.assertEqual(code, 500)
            self.assertFalse(payload.get("ok"))
            self.assertIn("observations_delete_scope_failed", payload.get("errors", []))
            # Partial completion must be explicit: results dict present.
            self.assertIn("observations", payload.get("results", {}))

    def test_control_export_blocked_is_explicit_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostObservationStore(Path(td))
            _write_mid_corrupted(store)
            surf = GhostControlSurface.from_state_home(Path(td))
            exported = surf.export_state()
            self.assertFalse(exported.get("ok"))
            self.assertTrue(exported.get("errors"))
            # Must not masquerade as empty success.
            obs = exported.get("observations")
            if isinstance(obs, dict):
                self.assertNotEqual(obs.get("observations"), [])


if __name__ == "__main__":
    unittest.main()
