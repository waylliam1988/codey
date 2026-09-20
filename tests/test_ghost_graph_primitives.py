"""Ghost graph primitives: one curve, two clamps, no personality loss."""

from __future__ import annotations

import unittest

from codey.ghost import graph_primitives as primitives


class GraphPrimitivesTests(unittest.TestCase):
    def test_parse_ts_roundtrip_and_fallback(self) -> None:
        self.assertEqual(
            primitives.parse_ts("2026-09-20T00:00:00Z").isoformat(),
            "2026-09-20T00:00:00+00:00",
        )
        self.assertEqual(
            primitives.parse_ts("2026-09-20T00:00:00+00:00").isoformat(),
            "2026-09-20T00:00:00+00:00",
        )
        # Unparseable input means "now", never an exception.
        self.assertIsNotNone(primitives.parse_ts("not-a-date"))

    def test_half_life_curve(self) -> None:
        self.assertAlmostEqual(primitives.exp_decay_factor(0.0, 90.0), 1.0)
        self.assertAlmostEqual(primitives.exp_decay_factor(90 * 86400.0, 90.0), 0.5)
        self.assertAlmostEqual(primitives.exp_decay_factor(180 * 86400.0, 90.0), 0.25)
        self.assertGreaterEqual(primitives.exp_decay_factor(-5.0, 90.0), 1.0 - 1e-9)

    def test_decay_basis_prefers_last_decay(self) -> None:
        self.assertEqual(primitives.decay_basis_of("d", "r", "u"), "d")
        self.assertEqual(primitives.decay_basis_of("", "r", "u"), "r")
        self.assertEqual(primitives.decay_basis_of("", "", "u"), "u")

    def test_raw_decay_matches_half_life_math(self) -> None:
        now = "2026-09-20T00:00:00Z"
        basis = "2026-06-22T00:00:00Z"  # 90 days earlier
        raw = primitives.decayed_by_half_life(1.0, basis, now, 90.0)
        self.assertAlmostEqual(raw, 0.5, places=3)

    def test_any_decay_due(self) -> None:
        now = "2026-09-20T00:00:00Z"
        self.assertTrue(
            primitives.any_decay_due([], now=now, min_interval_seconds=0)
        )
        self.assertFalse(
            primitives.any_decay_due([], now=now, min_interval_seconds=60)
        )
        self.assertTrue(
            primitives.any_decay_due(
                ["2020-01-01T00:00:00Z"], now=now, min_interval_seconds=60
            )
        )
        self.assertFalse(
            primitives.any_decay_due([now], now=now, min_interval_seconds=3600)
        )

    def test_stores_delegate_without_changing_clamps(self) -> None:
        from codey.ghost import affinity as affinity_module
        from codey.ghost import hebbian as hebbian_module

        now = "2026-09-20T00:00:00Z"
        basis = "2026-06-22T00:00:00Z"
        # Same curve, each store keeps its own clamp edge semantics.
        hebbian = hebbian_module._decayed_weight(0.8, basis, now, 90.0)
        raw = primitives.decayed_by_half_life(0.8, basis, now, 90.0)
        self.assertAlmostEqual(
            hebbian, round(max(0.0, min(1.0, raw)), 6), places=6
        )
        affinity = affinity_module._decayed_weight(0.8, basis, now, 90.0)
        from codey.ghost.numbers import clamp_unit_float

        self.assertEqual(affinity, clamp_unit_float(raw, digits=6))
        # Bool edge stays store-local: Hebbian rounds True->1.0, affinity
        # clamps True->0.0. Primitives must not paper over the difference.
        self.assertEqual(affinity_module._unit_float(True), 0.0)


if __name__ == "__main__":
    unittest.main()
