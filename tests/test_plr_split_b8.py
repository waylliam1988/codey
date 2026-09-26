"""PLR split B8 regression: pure-extraction equivalence for five targets.

Covers helpers introduced to bring PLR0912/PLR0915 under limits with zero
behavior change. No deterministic product bugs were found during the hunt
(see summary); these tests lock in current semantics.
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from codey.automation.browser_worker import (
    BrowserWorker,
    _abandon_browser_job,
    _call_deadlines,
    _Job,
    _JobState,
)
from codey.providers.diagnostics import ProviderActionError
from codey.providers.discovery import (
    _combined_text,
    _fingerprint,
    _score_message_box_candidate,
    _score_send_button_candidate,
    score_control_candidate,
)
from codey.providers.worker import (
    WorkerChatProvider,
    _PendingRequest,
    _WorkerSession,
)
from codey.runs.ledger import SCHEMA_VERSION, RunLedgerRecord
from codey.runs.ledger_projection import (
    _apply_command_verified,
    _apply_file_changed,
    _apply_run_finished,
    _apply_run_started,
    _apply_tool_finished,
    _LedgerBuildState,
    _track_ledger_provider,
    project_run_ledger,
)


def _record(seq: int, event_type: str, **fields: object) -> RunLedgerRecord:
    return RunLedgerRecord({
        "schema_version": SCHEMA_VERSION,
        "seq": seq,
        "ts": f"2026-07-28T00:00:{seq:02d}Z",
        "type": event_type,
        "run_id": "run-1",
        "session_id": "session-1",
        **fields,
    })


class DiscoverySplitTests(unittest.TestCase):
    def test_message_box_dispatch_matches_helper(self) -> None:
        candidate = {
            "visible": True,
            "bottom_ratio": 0.8,
            "area": 2000,
            "fingerprint": {"tag": "textarea", "ariaLabel": "message input"},
        }
        fp = _fingerprint(candidate["fingerprint"])
        text = _combined_text(fp)
        self.assertEqual(
            score_control_candidate(candidate, "message_box"),
            _score_message_box_candidate(candidate, fp, text),
        )
        self.assertGreater(score_control_candidate(candidate, "message_box"), 0)

    def test_send_button_dispatch_matches_helper(self) -> None:
        candidate = {
            "visible": True,
            "enabled": True,
            "bottom_ratio": 0.9,
            "fingerprint": {"tag": "button", "text": "Send message"},
        }
        fp = _fingerprint(candidate["fingerprint"])
        text = _combined_text(fp)
        self.assertEqual(
            score_control_candidate(candidate, "send_button"),
            _score_send_button_candidate(candidate, fp, text, "send_button", None),
        )
        self.assertGreater(score_control_candidate(candidate, "send_button"), 0)

    def test_invisible_scores_negative(self) -> None:
        candidate = {"visible": False, "fingerprint": {"tag": "textarea"}}
        self.assertEqual(score_control_candidate(candidate, "message_box"), -1000)
        self.assertEqual(score_control_candidate(candidate, "send_button"), -1000)

    def test_unknown_action_scores_negative(self) -> None:
        candidate = {"visible": True, "fingerprint": {"tag": "button", "text": "Send"}}
        self.assertEqual(score_control_candidate(candidate, "nope"), -1000)


class BrowserWorkerSplitTests(unittest.TestCase):
    def test_call_deadlines_combines_timeout_and_caller(self) -> None:
        _event, active = _call_deadlines(None)
        self.assertIsNone(active)
        _event2, active2 = _call_deadlines(5.0)
        self.assertIsNotNone(active2)

    def test_abandon_transitions(self) -> None:
        queued = _Job(fn=lambda: None, args=(), kwargs={})
        _abandon_browser_job(queued)
        self.assertTrue(queued.abandoned)
        self.assertEqual(queued.state, _JobState.CANCELLED)

        running = _Job(fn=lambda: None, args=(), kwargs={}, state=_JobState.RUNNING)
        _abandon_browser_job(running)
        self.assertEqual(running.state, _JobState.ABANDONED)

    def test_call_still_executes(self) -> None:
        worker = BrowserWorker(name="test-plr-b8-call")
        try:
            self.assertEqual(worker.call(lambda: 7), 7)
            self.assertEqual(worker.call(lambda x: x + 1, 41), 42)
        finally:
            worker.close(timeout=5.0)
            self.assertFalse(worker._thread.is_alive())

    def test_reentrant_call_works(self) -> None:
        worker = BrowserWorker(name="test-plr-b8-reentrant")
        try:
            seen: list[int] = []

            def outer() -> int:
                seen.append(threading.get_ident())
                return worker.call(lambda: 999, timeout=2.0)

            self.assertEqual(worker.call(outer, timeout=5.0), 999)
            self.assertEqual(len(seen), 1)
        finally:
            worker.close(timeout=5.0)


class LedgerSplitTests(unittest.TestCase):
    def test_apply_run_started_tracks_provider(self) -> None:
        state = _LedgerBuildState()
        _apply_run_started(state, {"ts": "t", "project": "p", "mode": "m",
                                   "task_chars": 9, "provider": "deepseek"})
        self.assertTrue(state.has_run_started)
        self.assertEqual(state.provider_initial, "deepseek")
        self.assertEqual(state.provider_final, "deepseek")
        self.assertEqual(state.task_chars, 9)

    def test_track_provider_keeps_first_initial(self) -> None:
        state = _LedgerBuildState()
        _track_ledger_provider(state, "a")
        _track_ledger_provider(state, "b")
        self.assertEqual(state.provider_initial, "a")
        self.assertEqual(state.provider_final, "b")

    def test_tool_file_command_helpers(self) -> None:
        state = _LedgerBuildState()
        _apply_tool_finished(state, {"tool": "edit", "ok": False})
        _apply_tool_finished(state, {"tool": "", "ok": True})
        self.assertEqual(state.tool_calls, 2)
        self.assertEqual(state.tool_errors, 1)
        _apply_file_changed(state, {"path": "a.py"})
        _apply_file_changed(state, {"path": "a.py"})
        self.assertEqual(state.changed_files, ["a.py"])
        _apply_command_verified(state, {"command": "pytest", "cwd": ".",
                                        "turn": 1, "tool_id": "1:0"})
        _apply_command_verified(state, {"command": "pytest", "cwd": ".",
                                        "turn": 2, "tool_id": "2:0"})
        _apply_command_verified(state, {"command": "", "cwd": "."})
        self.assertEqual(len(state.verified_commands), 1)

    def test_run_finished_helper(self) -> None:
        state = _LedgerBuildState()
        _apply_run_finished(state, {"ts": "t", "stop_reason": "done",
                                    "turns": 3, "max_turns": 8, "provider": "q"})
        self.assertTrue(state.has_run_finished)
        self.assertEqual(state.provider_final, "q")

    def test_end_to_end_projection(self) -> None:
        records = [
            _record(1, "run_started", project="p", mode="m", provider="a", task_chars=4),
            _record(2, "tool_finished", tool="edit", ok=True),
            _record(3, "file_changed", path="a.py"),
            _record(4, "provider_switched", from_provider="a", to_provider="b",
                    phase="x", reason="y"),
            _record(5, "run_finished", provider="b", stop_reason="done",
                    turns=2, max_turns=8),
        ]
        projection = project_run_ledger(records)
        self.assertTrue(projection.complete)
        self.assertEqual(projection.provider_initial, "a")
        self.assertEqual(projection.provider_final, "b")
        self.assertEqual(projection.tool_calls, 1)
        self.assertEqual(projection.changed_files_observed, ("a.py",))


class ProviderWorkerSplitTests(unittest.TestCase):
    def test_resolve_done_ok_returns_result(self) -> None:
        provider = WorkerChatProvider.__new__(WorkerChatProvider)
        provider.provider_id = "p"

        pending = _PendingRequest(request_id="r", method="send")
        session = _WorkerSession(proc=None, job=None)  # type: ignore[arg-type]
        result = provider._resolve_done_pending(
            session, pending, {"ok": True, "result": "hello"},
            "", False, "",
        )
        self.assertEqual(result, "hello")

    def test_resolve_done_failure_raises(self) -> None:
        provider = WorkerChatProvider.__new__(WorkerChatProvider)
        provider.provider_id = "p"
        pending = _PendingRequest(request_id="r", method="send")
        session = _WorkerSession(proc=None, job=None)  # type: ignore[arg-type]
        with self.assertRaises(ProviderActionError):
            provider._resolve_done_pending(
                session, pending, {"ok": False, "error": "bad"},
                "", False, "",
            )

    def test_drain_exited_raises_when_reader_drained(self) -> None:
        provider = WorkerChatProvider.__new__(WorkerChatProvider)
        provider.provider_id = "p"
        provider._life_lock = threading.Lock()
        provider._session = None
        pending = _PendingRequest(request_id="r", method="send")
        session = _WorkerSession(proc=None, job=None)  # type: ignore[arg-type]
        session.stderr_tail.clear()
        with self.assertRaises(ProviderActionError):
            provider._drain_exited_child(session, pending, 5.0, True, None)


class AgentLoopSplitTests(unittest.TestCase):
    def test_track_continue_progress_resets_on_progress(self) -> None:
        from codey.agents.loop import _track_continue_progress

        class _Cfg:
            stagnant_turns = 3

        class _Stag:
            count = 2

        class _Sess:
            config = _Cfg()
            stagnation = _Stag()

        class _State:
            made_progress = True

        class _Control:
            body = "x"

        sess = _Sess()
        self.assertIsNone(_track_continue_progress(sess, _State(), _Control(), 1))  # type: ignore[arg-type]
        self.assertEqual(sess.stagnation.count, 0)

    def test_track_continue_progress_counts_and_stops(self) -> None:
        from codey.agents.loop import _track_continue_progress

        class _Cfg:
            stagnant_turns = 2

        class _Sess:
            def __init__(self) -> None:
                from codey.agents.loop import _finish  # noqa: F401
                self.config = _Cfg()
                self.stagnation = type("S", (), {"count": 0})()
                self.request = type("R", (), {"conversation": None})()
                self.verification = type("V", (), {"checks_passed": False})()
                self.progress = type("P", (), {"wrote_files": False})()

        class _State:
            made_progress = False

        class _Control:
            body = "msg"

        sess = _Sess()
        # First no-progress turn: counts but does not stop.
        import codey.agents.loop as loop_mod

        with mock.patch.object(loop_mod, "emit"):
            out = _track_continue_progress(sess, _State(), _Control(), 1)  # type: ignore[arg-type]
        self.assertIsNone(out)
        self.assertEqual(sess.stagnation.count, 1)

    def test_check_runaway_guard_no_block(self) -> None:
        from codey.agents.loop import _check_runaway_guard

        class _Sess:
            stagnation = type("S", (), {"attempts": []})()

        with mock.patch("codey.agents.runaway_guard.should_block_or_remind",
                        return_value=None):
            reason, stop = _check_runaway_guard(_Sess())  # type: ignore[arg-type]
        self.assertEqual((reason, stop), ("", ""))


if __name__ == "__main__":
    unittest.main()
