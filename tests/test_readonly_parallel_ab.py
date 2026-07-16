from __future__ import annotations

from pathlib import Path

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


def test_deterministic_probe_observes_read_files_speedup_and_preserves_order() -> None:
    report = readonly_parallel_ab.run_deterministic(
        repeats=1,
        delay=0.03,
        max_workers=4,
    )

    read_files = _comparison(report, "read_files_4")
    concurrent = _row(report, "read_files_4", "concurrent")

    assert read_files["meaningful_speedup"]
    assert read_files["correctness_ok"]
    assert concurrent["flags"]["result_order_ok"]
    assert [item["tool"] for item in concurrent["sample_trace"]] == [
        "read",
        "read",
        "read",
        "read",
    ]


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
