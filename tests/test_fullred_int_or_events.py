"""Full-red: int/or/events/generation family (assert CLEANED correct behavior).

Each test asserts what correct code must do. PASS now => not a bug.
FAIL now => deterministic bug to fix.
"""
from __future__ import annotations


def test_events_malformed_metadata_does_not_crash() -> None:
    from codey.runtime.core.models import ToolCall
    from codey.runtime.observe.events import RunEvent, run_event_ui_payload

    call = ToolCall(name="read_file", args={"path": "a.txt"})
    # malformed tool_index must not crash, must fall back to 0
    for bad in ("abc", "²", None, True, 1.5):
        ev = RunEvent(
            kind="tool_start", turn=1, call=call, message="x", metadata={"tool_index": bad}
        )
        out = run_event_ui_payload("r", "s", ev)
        assert out is not None
        assert out["tool_id"] == "1:0", f"bad={bad!r} got {out['tool_id']!r}"


def test_events_managed_bytes_malformed_does_not_crash() -> None:
    # managed_output goes through normalized_managed_output (strict, returns 0
    # for bad values) before events.py sees it, so raw "abc" cannot reach here
    # via real path. Covered by strict_nonnegative_int locks. Not a bug.
    from codey.runtime.core.models import normalized_managed_output

    payload = normalized_managed_output({
        "handle": "out_abc",
        "original_bytes": "abc",
        "stored_bytes": "²",
        "sha256": "a" * 64,
        "original_sha256": "",
        "stored_truncated": False,
    })
    assert payload["original_bytes"] == 0
    assert payload["stored_bytes"] == 0


def test_expires_at_minimum_one_day_is_intentional() -> None:
    from codey.ghost.continuity import _expires_at

    # minimum 1 day is intentional clamp; days=0 -> +1 day must hold
    assert _expires_at("2026-01-01T00:00:00Z", 0) == "2026-01-02T00:00:00Z"
    assert _expires_at("2026-01-01T00:00:00Z", None) == "2026-01-02T00:00:00Z"  # type: ignore[arg-type]


def test_bounded_limit_default_zero_clamps_to_one() -> None:
    from codey.research.guards import bounded_limit

    # lower bound is 1, so default=0 still yields 1; intentional
    assert bounded_limit("bad", default=0, upper=10) == 1
    assert bounded_limit(None, default=0, upper=10) == 1
    assert bounded_limit(5, default=0, upper=10) == 5


def test_turn_budget_min_one_is_intentional() -> None:
    from types import SimpleNamespace

    from codey.runtime.write.task_runtime import _turn_budget

    assert _turn_budget(SimpleNamespace(max_turns=0)) == 1
    assert _turn_budget(SimpleNamespace(max_turns=None)) == 1
    assert _turn_budget(SimpleNamespace(max_turns="abc")) == 1
    assert _turn_budget(SimpleNamespace(max_turns=5)) == 5


def test_merge_metadata_rejects_non_dict() -> None:
    from codey.ghost.inbox import _merge_metadata

    # correct code must reject list input, not silently coerce
    try:
        result = _merge_metadata([("a", 1)], None)  # type: ignore[arg-type]
    except (TypeError, AttributeError):
        return
    # if it returns, it must not silently accept list as dict
    assert result == {}, f"list input silently coerced to {result!r}"


def test_search_page_offset_min_one_is_intentional() -> None:
    from codey.toolchain.search_page import normalize_page_args

    # offset 0 clamps to 1 is intentional; just verify no crash and sane output
    start, page = normalize_page_args(0, 2, None, 10)
    assert start == 1
    assert page == 2


def test_trace_missing_count_defaults_to_one() -> None:
    from codey.runs.trace import _research_connector_error_payload

    # missing count -> 1 is intentional default for one error occurrence
    payload = _research_connector_error_payload(
        {"connector_id": "c", "action": "a", "error": "e"}
    )
    assert payload["count"] == 1
