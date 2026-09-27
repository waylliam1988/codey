"""Full-red round3: OverflowError + hostname + bad-row + ascii-digit family.

Each test asserts what correct code must do. PASS now => not a bug.
FAIL now => deterministic bug to fix (red-first).
"""
from __future__ import annotations


def test_ledger_tool_id_malformed_does_not_crash() -> None:
    from codey.runs.ledger import _tool_id
    from codey.runtime.observe.events import RunEvent

    for bad in ("abc", "²", True, float("inf"), 1.5, None):
        ev = RunEvent(kind="tool", turn=3, metadata={"tool_index": bad})
        assert _tool_id(ev) == "3:0", f"bad={bad!r}"


def test_events_safe_byte_count_overflow_does_not_crash() -> None:
    from codey.runtime.observe.events import _safe_byte_count

    assert _safe_byte_count(float("inf")) == 0
    assert _safe_byte_count(float("-inf")) == 0
    assert _safe_byte_count("abc") == 0
    assert _safe_byte_count(True) == 0
    assert _safe_byte_count(10) == 10


def test_search_page_footer_malformed_does_not_crash() -> None:
    from codey.toolchain.search_page import page_footer

    for bad in ("abc", float("inf"), "2.0", None):
        out = page_footer(query="q", path="p", offset=bad, limit=10, shown=1, has_more=False)  # type: ignore[arg-type]
        assert "results 1-1" in out, f"bad={bad!r} got {out!r}"


def test_changes_numstat_unicode_does_not_crash() -> None:
    from codey.workspace.changes import _merge_numstat

    stats: dict[str, dict[str, int]] = {}
    _merge_numstat(stats, "2\t5\tfoo.py")
    assert stats == {"foo.py": {"additions": 2, "deletions": 5}}
    stats2: dict[str, dict[str, int]] = {}
    _merge_numstat(stats2, "²\t5\tfoo.py")
    assert stats2 == {"foo.py": {"additions": 0, "deletions": 5}}
    arabic = chr(0x0661) + chr(0x0662) + chr(0x0663)
    stats3: dict[str, dict[str, int]] = {}
    _merge_numstat(stats3, arabic + "\t5\tfoo.py")
    assert stats3 == {"foo.py": {"additions": 0, "deletions": 5}}


def test_guards_bounded_int_overflow_does_not_crash() -> None:
    from codey.research.guards import bounded_int

    assert bounded_int(float("inf"), 1, 5) == 1
    assert bounded_int(float("-inf"), 1, 5) == 1
    assert bounded_int("abc", 1, 5) == 1


def test_work_queue_int_overflow_does_not_crash() -> None:
    from codey.ghost.work_queue import _int as _wq_int

    assert _wq_int(float("inf")) == 0
    assert _wq_int("abc") == 0
    assert _wq_int(3) == 3


def test_inbox_int_or_default_overflow_does_not_crash() -> None:
    from codey.ghost.inbox import _int_or_default

    assert _int_or_default(float("inf"), 1) == 1
    assert _int_or_default("abc", 7) == 7
    assert _int_or_default(3, 1) == 3


def test_trace_int_overflow_does_not_crash() -> None:
    from codey.runs.trace import _bounded_int as _tr_bounded
    from codey.runs.trace import _nonnegative_int as _tr_nn

    assert _tr_nn(float("inf")) == 0
    assert _tr_bounded(float("inf"), 1, 5) == 1
    assert _tr_nn("abc") == 0


def test_controller_as_int_overflow_does_not_crash() -> None:
    from codey.research.controller import _as_int, _as_optional_int

    assert _as_int(float("inf")) == 0
    assert _as_int(float("inf"), default=7) == 7
    assert _as_optional_int(float("inf")) is None
    assert _as_int(3) == 3


def test_refs_nonnegative_int_overflow_does_not_crash() -> None:
    from codey.utils.refs import nonnegative_int

    assert nonnegative_int(float("inf")) == 0
    assert nonnegative_int(float("-inf")) == 0
    assert nonnegative_int("abc") == 0


def test_positive_int_overflow_does_not_crash() -> None:
    from codey.utils.positive_int import positive_int

    assert positive_int(float("inf")) is None
    assert positive_int(float("-inf")) is None
    assert positive_int("abc") is None
    assert positive_int(5) == 5


def test_revision_overflow_does_not_crash() -> None:
    from codey.workspace.revision import valid_workspace_revision

    assert valid_workspace_revision(float("inf")) == 0
    assert valid_workspace_revision(float("-inf")) == 0
    assert valid_workspace_revision("abc") == 0


def test_change_set_nonnegative_overflow_does_not_crash() -> None:
    from codey.workspace.change_set import _nonnegative_int as _cs_nn

    assert _cs_nn(float("inf")) == 0
    assert _cs_nn("abc") == 0


def test_conversation_store_int_overflow_does_not_crash() -> None:
    from codey.storage.conversation_store import _nonnegative_int as _conv_nn
    from codey.storage.conversation_store import _positive_int as _conv_pos

    assert _conv_nn(float("inf")) == 0
    assert _conv_pos(float("inf"), 5) == 5


def test_api_max_turns_overflow_is_400_not_500() -> None:
    from codey.app.api import run_submit_response

    def _fake(*args: object, **kwargs: object) -> str | None:
        return "r1"

    code, _ = run_submit_response(
        {
            "session_id": "s",
            "project": "p",
            "task": "t",
            "provider": "deepseek",
            "intent": "auto",
            "max_turns": float("inf"),
        },
        _fake,  # type: ignore[arg-type]
    )
    assert code == 400


def test_api_base_revision_overflow_is_400_not_500() -> None:
    from types import SimpleNamespace

    from codey.app.api import save_ui_state_response

    ctx = SimpleNamespace(
        save_ui_state=lambda state, base_revision: {"revision": 1},
        load_ui_state=lambda: {},
    )
    code, _ = save_ui_state_response(ctx, {"state": {}, "base_revision": float("inf")})
    assert code == 400


def test_event_bus_replay_malformed_does_not_crash() -> None:
    from codey.app.event_bus import EventBus

    bus = EventBus(replay_limit=10)
    assert bus.replay_events_after("abc") == []  # type: ignore[arg-type]
    assert bus.replay_events_after(float("inf")) == []  # type: ignore[arg-type]


def test_local_config_window_malformed_rejects_cleanly() -> None:
    from codey.providers.local_config import context_budget_for_window

    for bad in ("8192x", "12.0", float("inf"), True, None):
        try:
            out = context_budget_for_window(bad)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            continue
        raise AssertionError(f"bad={bad!r} should raise, got {out!r}")


def test_browser_search_evil_subdomain_is_public() -> None:
    from codey.research.browser_search import _looks_like_public_result_url

    assert _looks_like_public_result_url("https://www.bing.com/search?q=x") is False
    assert _looks_like_public_result_url("https://bing.com.evil.example/search") is True
    assert _looks_like_public_result_url("https://duckduckgo.com.evil.example/html/") is True


def test_controls_host_port_and_direction() -> None:
    from codey.providers.controls import _host_matches, _page_host

    class _P:
        def __init__(self, url: str) -> None:
            self.url = url

    assert _page_host(_P("https://chat.deepseek.com:8443/")) == "chat.deepseek.com"
    assert _page_host(_P("https://chat.deepseek.com/")) == "chat.deepseek.com"
    assert _host_matches("sub.evil.com", "evil.com") is True
    assert _host_matches("evil.com", "sub.evil.com") is False
    assert _host_matches("chat.deepseek.com", "chat.deepseek.com") is True


def test_observation_index_skips_bad_rows() -> None:
    from codey.ghost.observation_index import _fit_blocks, retrieve_relevant_observations

    good = {
        "user_text": "hi",
        "assistant_text": "yo",
        "ts": "2026-01-01T00:00:00Z",
        "run_id": "r1",
        "mode": "chat",
    }
    rows = [good, None, 123, "oops"]  # type: ignore[list-item]
    out = retrieve_relevant_observations(rows, "hi", budget_chars=1000, max_items=5)  # type: ignore[arg-type]
    assert isinstance(out, tuple)
    assert all(isinstance(r, dict) for r in out)
    out2 = _fit_blocks([good], 1000, 5)
    assert len(out2) == 1


def test_ascii_digit_gate_rejects_non_ascii() -> None:
    from codey.agents.protocol import positive_int_value
    from codey.agents.shell_approval import _nonnegative_int as _shell_nn
    from codey.providers.local_config import _parse_positive_int
    from codey.toolchain.runtime import bounded_positive_int
    from codey.toolchain.tool_args_repair import ToolArgsRepairError
    from codey.toolchain.tool_args_repair import _bounded_positive_int as _repair_bpos
    from codey.utils.refs import strict_nonnegative_int
    from codey.workspace.config import _positive_int as _cfg_pos

    arabic = chr(0x0661) + chr(0x0662) + chr(0x0663)
    sup2 = chr(0xB2)
    assert _parse_positive_int(arabic) is None
    assert _parse_positive_int(sup2) is None
    assert positive_int_value(arabic) is None
    assert positive_int_value(sup2) is None
    assert strict_nonnegative_int(arabic) == 0
    assert strict_nonnegative_int(sup2) == 0
    assert _shell_nn(arabic) == 0
    assert _shell_nn(sup2) == 0
    assert _cfg_pos(arabic) is None
    assert _cfg_pos(sup2) is None
    for bad in (arabic, sup2):
        try:
            bounded_positive_int(bad, "offset")
        except ValueError:
            pass
        else:
            raise AssertionError(f"bad digit should raise, got {bad!r}")
        try:
            _repair_bpos(bad, "offset")
        except ToolArgsRepairError:
            pass
        else:
            raise AssertionError(f"repair bad digit should raise, got {bad!r}")
    assert bounded_positive_int("12", "offset") == 12
