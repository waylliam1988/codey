"""P1 hardening: revival locking, supervisor save, deadline, B023."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from codey.providers import revival
from codey.providers.diagnostics import FAILURE_TRANSIENT, ProviderFailure
from codey.providers.supervisor import ProviderSupervisor, run_half_open_canary
from codey.runtime.core import cancellation


class RevivalLockingTests(unittest.TestCase):
    def test_concurrent_complete_send_does_not_lose_updates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "revival.json"
            barrier = threading.Barrier(2)
            errors: list[BaseException] = []

            def _write(tag: str) -> None:
                try:
                    barrier.wait(timeout=5)
                    revival.complete_send(
                        path,
                        "qwen",
                        "host",
                        {"message_box": {"selector": tag}},
                        verified=set(),
                        learned_verified=set(),
                    )
                except BaseException as exc:  # noqa: BLE001 -- collected
                    errors.append(exc)

            threads = [
                threading.Thread(target=_write, args=(f"sel-{i}",), daemon=True)
                for i in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(errors, [])
            data = revival._load_store(path)
            self.assertIn("qwen", data)

    def test_record_paths_hold_file_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "revival.json"
            revival.complete_send(
                path, "qwen", "host", {"message_box": {"s": "a"}},
                verified=set(), learned_verified=set(),
            )
            with mock.patch(
                "codey.providers.revival.with_file_lock",
                wraps=revival.with_file_lock,
            ) as locked:
                revival.record_control_success(path, "qwen", "message_box")
                revival.record_control_failure(path, "qwen", "message_box")
                revival.record_flow_failure(path, "qwen")
                revival.load_flow_recipe(path, "qwen", "")
            self.assertTrue(locked.called)


class SupervisorSaveTests(unittest.TestCase):
    def test_save_failure_is_surfaced_not_swallowed(self) -> None:
        from codey.providers.supervisor import HealthStoreError

        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td)
            # Explicit failure, not a silent best-effort: the event is
            # reported as unrecorded and memory rolls back to disk.
            with (
                mock.patch(
                    "codey.providers.supervisor.write_json_atomic",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaises(HealthStoreError),
            ):
                supervisor.record_success("qwen")
            self.assertIn("disk full", supervisor.last_save_error)
            self.assertNotIn("qwen", supervisor._health)

    def test_save_success_clears_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td)
            supervisor._last_save_error = "stale"
            supervisor.record_success("qwen")
            self.assertEqual(supervisor.last_save_error, "")

    def test_select_does_not_hold_lock_during_probe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td)
            supervisor.record_success("qwen")
            seen_locked: list[bool] = []
            real_is_available = ProviderSupervisor.is_available

            def _spy(self: ProviderSupervisor, provider_id: str) -> bool:
                seen_locked.append(self._lock.locked())
                return real_is_available(self, provider_id)

            with mock.patch.object(ProviderSupervisor, "is_available", _spy):
                supervisor.select("qwen", ["qwen"])
            # select() refreshes under the locks, then decides from its own
            # snapshot after releasing them. No deadlock even under contention.
            self.assertNotIn(True, seen_locked)

    def test_canary_deadline_is_transient_not_generic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td, clock=lambda: 100.0)
            supervisor.record_failure(
                "qwen",
                ProviderFailure("qwen", "send", "", "", "boom", "", FAILURE_TRANSIENT),
            )
            supervisor.record_failure(
                "qwen",
                ProviderFailure("qwen", "send", "", "", "boom", "", FAILURE_TRANSIENT),
            )
            provider = mock.Mock()
            provider.new_chat.side_effect = cancellation.DeadlineExceeded("budget gone")
            with mock.patch.object(
                supervisor, "record_canary_failure", wraps=supervisor.record_canary_failure
            ) as recorded:
                self.assertFalse(run_half_open_canary("qwen", provider, supervisor))
            failure = recorded.call_args.args[1]
            self.assertEqual(failure.kind, FAILURE_TRANSIENT)
            self.assertIn("budget", failure.message)

    def test_canary_cancel_still_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            supervisor = ProviderSupervisor(td)
            supervisor.record_failure(
                "qwen",
                ProviderFailure("qwen", "send", "", "", "boom", "", FAILURE_TRANSIENT),
            )
            provider = mock.Mock()
            provider.new_chat.side_effect = cancellation.TaskCancelled("stop")
            with self.assertRaises(cancellation.TaskCancelled):
                run_half_open_canary("qwen", provider, supervisor)


class KnowledgeIndexCloseTests(unittest.TestCase):
    def test_writes_fail_closed_after_close(self) -> None:
        from codey.knowledge.index import KnowledgeIndex
        from codey.knowledge.note import KnowledgeNote

        with tempfile.TemporaryDirectory() as td:
            index = KnowledgeIndex(Path(td) / "index.db")
            index.close()
            note = KnowledgeNote.create(
                type="fact",
                title="t",
                body="b",
                session_id="s1",
            )
            with self.assertRaises(RuntimeError):
                index.upsert(note, path="t.md", content_hash="h")
            with self.assertRaises(RuntimeError):
                index.remove("missing")
            with self.assertRaises(RuntimeError):
                index.clear()
            with self.assertRaises(RuntimeError):
                index.get("missing")
            with self.assertRaises(RuntimeError):
                index.add_link("a", "b")
            with self.assertRaises(RuntimeError):
                index.replace_links_touching(["a"], [])
            index.close()  # idempotent


class ClosureCaptureTests(unittest.TestCase):
    def test_each_sibling_sends_through_its_own_helper(self) -> None:
        """Behavioral B023: the per-iteration send lambda must close over the
        current loop helper, not the last one. Two providers are tried in
        sequence; each helper must receive exactly its own send."""
        from types import SimpleNamespace

        from codey.app import context as app_context
        from codey.app import sibling_probe
        from codey.providers import profile_doctor

        helpers: dict[str, object] = {}

        def _borrow(provider_id: str, _page: object):
            helper = mock.Mock()
            helper.send.side_effect = (
                lambda prompt, timeout=None, _id=provider_id: f"reply-from-{_id}"
            )
            helpers[provider_id] = helper
            return helper

        supervisor = mock.Mock()
        supervisor.is_available.return_value = True
        ctx = SimpleNamespace(
            providers=SimpleNamespace(supervisor=supervisor),
            set_provider_session=mock.Mock(),
        )
        request = profile_doctor.ProfileDoctorRequest(
            provider_id="deepseek",
            action="send",
            candidates=(),
            page=mock.Mock(),
        )
        seen: list[str] = []

        def _choose(_request, send):
            seen.append(send("probe"))
            return None

        with (
            mock.patch.object(
                sibling_probe, "borrow_open_provider", side_effect=_borrow
            ),
            mock.patch.object(profile_doctor, "choose_candidate", side_effect=_choose),
        ):
            self.assertIsNone(sibling_probe.handle_profile_doctor(ctx, request))
        # With late binding every send would hit the last loop helper; bound
        # lambdas hit exactly their own helper, once each.
        self.assertEqual(len(seen), 3)
        for provider_id, helper in helpers.items():
            helper.send.assert_called_once()
            self.assertIn(f"reply-from-{provider_id}", seen)
        self.assertTrue(callable(sibling_probe.handle_profile_doctor))
        self.assertTrue(callable(sibling_probe.handle_flow_recovery))
        self.assertFalse(hasattr(app_context.AppContext, "handle_profile_doctor"))
        self.assertFalse(hasattr(app_context.AppContext, "handle_flow_recovery"))

    def test_mimo_lambda_binds_builtin_ready(self) -> None:
        import inspect

        import codey.providers.web_drivers.mimo as mimo

        source = inspect.getsource(mimo)
        self.assertIn("built_in_ready=built_in_ready", source)


if __name__ == "__main__":
    unittest.main()
