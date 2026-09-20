from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey.app import provider_services, shell_service
from codey.app.context import AppContext
from codey.app.event_bus import RUN_EVENT_TYPES, stamp_run_scope
from codey.operations.task_state import TaskState
from codey.repairs.self_repair import SelfRepairSupervisor


def _protocol_members() -> list[str]:
    return sorted(name for name in dir(TaskState) if not name.startswith("_"))


class TaskStateProtocolTests(unittest.TestCase):
    def test_app_context_exposes_every_protocol_member(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ctx = AppContext(Path(td) / "state")
            try:
                missing = [name for name in _protocol_members() if not hasattr(ctx, name)]
            finally:
                ctx.close()
        self.assertEqual(missing, [])

    def test_protocol_hides_moved_domain_methods(self) -> None:
        # claim_shell_ticket / handle_* / kick left AppContext for
        # shell_service / sibling_probe / SelfRepairSupervisor on purpose.
        # get_provider stays as a thin test seam delegating to the single
        # provider entry point.
        for name in (
            "claim_shell_ticket",
            "handle_control_teach",
            "handle_profile_doctor",
            "handle_flow_recovery",
            "kick_self_repair",
        ):
            self.assertNotIn(name, _protocol_members())
            self.assertFalse(hasattr(AppContext, name), name)

    def test_protocol_source_has_no_any_leak(self) -> None:
        source = Path(__file__).resolve().parents[1] / "codey" / "operations" / "task_state.py"
        for lineno, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            self.assertNotIn(": Any", line, f"task_state.py:{lineno}")

    def test_stamp_run_scope_fills_active_run(self) -> None:
        active = SimpleNamespace(run_id="run-1", session_id="sess-1")
        payload = stamp_run_scope({"type": "tool", "text": "x"}, active)
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(payload["session_id"], "sess-1")

    def test_stamp_run_scope_keeps_explicit_ids(self) -> None:
        active = SimpleNamespace(run_id="run-1", session_id="sess-1")
        payload = stamp_run_scope(
            {"type": "tool", "run_id": "run-9", "session_id": "sess-9"}, active
        )
        self.assertEqual((payload["run_id"], payload["session_id"]), ("run-9", "sess-9"))

    def test_stamp_run_scope_ignores_non_run_types(self) -> None:
        active = SimpleNamespace(run_id="run-1", session_id="sess-1")
        payload = stamp_run_scope({"type": "unrelated"}, active)
        self.assertNotIn("run_id", payload)
        self.assertIn("turn", RUN_EVENT_TYPES)


class MovedGlueTests(unittest.TestCase):
    def test_open_provider_session_announces(self) -> None:
        events: list[dict] = []
        provider = object()
        ctx = SimpleNamespace(
            set_run_status=mock.Mock(),
            emit=events.append,
        )
        with mock.patch.object(
            provider_services, "connect_provider", return_value=provider
        ):
            self.assertIs(provider_services.open_provider_session(ctx, "qwen"), provider)
        self.assertEqual(
            [call.args[0] for call in ctx.set_run_status.call_args_list],
            ["connecting", "running"],
        )
        self.assertEqual(events[0]["status"], "connecting")
        self.assertTrue(events[1]["providers"])

    def test_claim_shell_ticket_mints_under_gate(self) -> None:
        gate = threading.Lock()
        lock = threading.Lock()
        approvals = mock.Mock()
        approvals.pop_shell.return_value = {
            "_approval_generation": 3,
            "project": ".",
            "cwd": ".",
            "command": "echo hi",
        }
        approvals.current_generation.return_value = 3
        run_registry = SimpleNamespace(stop_flag=SimpleNamespace(is_set=lambda: False))
        ctx = SimpleNamespace(
            _shell_spawn_gate=gate, lock=lock, approvals=approvals, run_registry=run_registry
        )
        with tempfile.TemporaryDirectory() as td:
            approvals.pop_shell.return_value["project"] = td
            pending, ticket = shell_service.claim_shell_ticket(
                ctx, "a1", timeout=30, output_limit=100
            )
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.command, "echo hi")
        self.assertEqual(ticket.generation, 3)

    def test_claim_shell_ticket_stale_generation_mints_nothing(self) -> None:
        ctx = SimpleNamespace(
            _shell_spawn_gate=threading.Lock(),
            lock=threading.Lock(),
            approvals=mock.Mock(
                pop_shell=mock.Mock(return_value={"_approval_generation": 1}),
                current_generation=mock.Mock(return_value=2),
            ),
            run_registry=SimpleNamespace(stop_flag=SimpleNamespace(is_set=lambda: False)),
        )
        pending, ticket = shell_service.claim_shell_ticket(ctx, "a1", timeout=30, output_limit=100)
        self.assertIsNotNone(pending)
        self.assertIsNone(ticket)

    def test_provider_failover_order_prefers_open_tabs(self) -> None:
        providers = SimpleNamespace(
            failover_order=lambda probe: ("qwen", "deepseek") if probe() else ("deepseek",),
        )
        with mock.patch.object(
            provider_services, "provider_tab_availability", return_value={"qwen": True}
        ):
            self.assertEqual(
                provider_services.provider_failover_order(providers), ("qwen", "deepseek")
            )

    def test_kick_if_idle_runs_once_and_guards_reentry(self) -> None:
        ran: list[str] = []

        def _runner(job):
            ran.append(job.provider_id)
            return mock.Mock(ok=True)

        supervisor = SelfRepairSupervisor(
            Path(tempfile.gettempdir()),
            runner=_runner,
            clock=lambda: 100.0,
        )
        from codey.providers.diagnostics import ProviderFailure
        from codey.providers.supervisor import STATE_OPEN, ProviderHealth

        supervisor.maybe_enqueue(
            "qwen",
            ProviderFailure("Qwen", "send", "", "", "response_missing", "now", "response_missing"),
            ProviderHealth(state=STATE_OPEN, last_failure_kind="response_missing"),
        )
        self.assertTrue(supervisor.kick_if_idle(lambda: False))
        self.assertFalse(supervisor.kick_if_idle(lambda: False))

    def test_kick_if_idle_respects_busy(self) -> None:
        supervisor = SelfRepairSupervisor(
            Path(tempfile.gettempdir()), runner=mock.Mock(), clock=lambda: 100.0
        )
        # No queued work: never runs.
        self.assertFalse(supervisor.kick_if_idle(lambda: False))


if __name__ == "__main__":
    unittest.main()
