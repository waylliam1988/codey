"""Retirement shape lock: control-surface export must not carry dead stores.

After the Ghost cold-start retirement (learning loop + pre-turn router gone),
export/delete_scope/reset must not mention ``router`` or ``signals``. The
schema version must bump so stale UI caches can detect the shape change.
"""

from __future__ import annotations

import tempfile
import unittest

from codey.ghost.control_surface import (
    CONTROL_SURFACE_SCHEMA_VERSION,
    GhostControlSurface,
)


class ControlSurfaceRetirementTests(unittest.TestCase):
    def test_schema_version_bumped_for_retired_shape(self) -> None:
        self.assertEqual(CONTROL_SURFACE_SCHEMA_VERSION, 2)

    def test_export_has_no_router_or_signals_keys(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            payload = GhostControlSurface.from_state_home(td).export_state()

        self.assertTrue(payload["ok"])
        self.assertNotIn("router", payload)
        self.assertNotIn("signals", payload)
        self.assertIn("inbox", payload)
        self.assertIn("sleep", payload)
        self.assertIn("observations", payload)

    def test_delete_scope_and_reset_have_no_router_or_signals(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            surface = GhostControlSurface.from_state_home(td)
            _, delete_payload = surface.dispatch_action({
                "action": "delete_scope",
                "scope": "user",
                "confirm": True,
            })
            _, reset_payload = surface.dispatch_action({
                "action": "reset_all",
                "confirm": True,
            })

        self.assertNotIn("router", delete_payload["results"])
        self.assertNotIn("signals", delete_payload["results"])
        self.assertNotIn("router", reset_payload["results"])
        self.assertNotIn("signals", reset_payload["results"])


if __name__ == "__main__":
    unittest.main()
