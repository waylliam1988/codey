from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from codey.runtime.core.models import ToolCall
from codey.toolchain.runtime import ToolOutcome
from tests.manual import readonly_parallel_ab


def _comparison(report: dict, case: str) -> dict:
    for item in report["comparisons"]:
        if item["case"] == case:
            return item
    raise AssertionError(f"missing comparison for {case}")


def _row(report: dict, case: str, arm: str) -> dict:
    for item in report["rows"]:
        if item["case"] == case and item["arm"] == arm:
            return item
    raise AssertionError(f"missing row for {case}/{arm}")


def test_deterministic_probe_preserves_order() -> None:
    # CI asserts correctness and ordered commit only. Wall-time speedup
    # (meaningful_speedup / improvement_ratio) stays in the repeated manual
    # performance report, never as a shared-machine CI gate.
    report = readonly_parallel_ab.run_deterministic(
        repeats=1,
        delay=0.03,
        max_workers=4,
    )

    read_files = _comparison(report, "read_files_4")
    concurrent = _row(report, "read_files_4", "concurrent")

    assert read_files["correctness_ok"]
    assert concurrent["flags"]["result_order_ok"]
    assert [item["tool"] for item in concurrent["sample_trace"]] == [
        "read",
        "read",
        "read",
        "read",
    ]


def test_concurrent_arm_enters_reads_simultaneously() -> None:
    # Barrier proof instead of a wall-time ratio: all four reads must be
    # inside at once, and results still commit in original order.
    barrier = threading.Barrier(4, timeout=10.0)

    class _BarrierTools(readonly_parallel_ab.SleepyToolFns):
        def read_file(self, _root: Path, rel: str, **_options: object) -> ToolOutcome:
            try:
                barrier.wait(timeout=10.0)
            except threading.BrokenBarrierError as exc:
                raise AssertionError("reads did not overlap") from exc
            return super().read_file(_root, rel)

    calls = tuple(
        ToolCall("read", {"path": name}) for name in ("a.py", "b.py", "c.py", "d.py")
    )
    with tempfile.TemporaryDirectory(prefix="codey-readonly-barrier-") as td:
        runner = readonly_parallel_ab.DeterministicBatchRunner(
            arm="concurrent",
            root=Path(td),
            tools=_BarrierTools(delay=0.01),
            existing_files=frozenset(),
            max_workers=4,
        )
        records = runner.run(calls)
    assert [record.index for record in records] == [0, 1, 2, 3]
    assert all(record.outcome.ok for record in records)
    assert barrier.n_waiting == 0


def test_flush_before_edit_keeps_read_before_edit_semantics() -> None:
    report = readonly_parallel_ab.run_deterministic(
        repeats=1,
        delay=0.02,
        max_workers=4,
    )

    concurrent = _row(report, "flush_before_edit", "concurrent")

    assert concurrent["flags"]["read_unlock_ok"]
    assert [item["tool"] for item in concurrent["sample_trace"]] == [
        "read",
        "read",
        "edit",
    ]


def test_references_boundary_stays_serial_in_concurrent_arm() -> None:
    report = readonly_parallel_ab.run_deterministic(
        repeats=1,
        delay=0.02,
        max_workers=4,
    )

    concurrent = _row(report, "references_boundary", "concurrent")

    assert concurrent["flags"]["references_non_overlapping"]
    assert [item["tool"] for item in concurrent["sample_trace"]] == [
        "search",
        "references",
        "read",
    ]


def test_timeout_provider_applies_defaults_when_agent_passes_none() -> None:
    class Provider:
        name = "Fake"
        location = "fake://provider"

        def __init__(self) -> None:
            self.new_chat_timeouts = []
            self.send_timeouts = []

        def new_chat(self, timeout=None) -> None:
            self.new_chat_timeouts.append(timeout)

        def send(self, text: str, timeout=None) -> str:
            self.send_timeouts.append(timeout)
            return text

        def close(self) -> None:
            pass

    inner = Provider()
    provider = readonly_parallel_ab.TimeoutCountingProvider(
        inner,
        send_timeout=12.0,
        new_chat_timeout=3.0,
    )

    provider.new_chat()
    provider.send("hello")

    assert inner.new_chat_timeouts == [3.0]
    assert inner.send_timeouts == [12.0]
    assert provider.sends == 1
    assert provider.sent_chars == 5
    assert provider.reply_chars == 5


def test_default_output_stays_outside_repository() -> None:
    assert readonly_parallel_ab.ROOT not in readonly_parallel_ab.DEFAULT_OUTPUT.parents
    assert isinstance(readonly_parallel_ab.DEFAULT_OUTPUT, Path)
