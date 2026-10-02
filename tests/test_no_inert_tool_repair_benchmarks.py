"""Retired version comparators must not be runnable cold-start benchmarks."""

from pathlib import Path


def test_no_inert_tool_repair_benchmarks_remain():
    manual = Path(__file__).parent / "manual"
    for name in (
        "tool_args_repair_smoke.py", "tool_args_repair_simulated_ab.py",
        "tool_args_repair_live_ab.py", "tool_args_repair_dialect_pressure_ab.py",
    ):
        assert not (manual / name).exists()
