from pathlib import Path

from codey.app.shell_service import ShellExecutionTicket, execute_shell_ticket
from codey.policies.command_line import resolve_shell
from codey.policies.shell_analysis import analyze_shell
from codey.storage.execution_output_store import ExecutionOutputStore
from tests.support.shell_execution import ShellExecutionHarness


class _OutputContext(ShellExecutionHarness):
    def __init__(self, store: ExecutionOutputStore) -> None:
        super().__init__()
        self.execution_outputs = store


class _UnavailableOutputStore(ExecutionOutputStore):
    def create(self, execution_id: str, *, session_id: str, run_id: str, max_bytes: int | None = None) -> None:
        raise OSError("output database unavailable")


def _ticket(cwd: Path, generation: int = 0) -> ShellExecutionTicket:
    shell = resolve_shell()
    command = "echo shell-output-probe"
    return ShellExecutionTicket(
        command, cwd, generation, 10, 4000, shell, analyze_shell(command, shell),
        execution_id="execution_probe", session_id="session_probe", run_id="run_probe",
    )


def test_output_store_failure_preserves_shell_result(tmp_path: Path) -> None:
    store = _UnavailableOutputStore(tmp_path)
    try:
        result = execute_shell_ticket(_OutputContext(store), _ticket(tmp_path))
        assert result["status"] == "exit", result
        assert result["exit_code"] == 0
        assert result["output"] == "shell-output-probe"
        assert "output_ref" not in result
    finally:
        store.close()


def test_shell_result_retains_successfully_observed_output(tmp_path: Path) -> None:
    store = ExecutionOutputStore(tmp_path)
    try:
        ticket = _ticket(tmp_path)
        result = execute_shell_ticket(_OutputContext(store), ticket)
        assert result["status"] == "exit", result
        assert result["full_output_retained"] is True
        assert result["output_ref"]["execution_id"] == ticket.execution_id
        snapshot = store.snapshot(ticket.execution_id)
        assert snapshot["execution_state"] == "settled"
        assert snapshot["stdout"]["head"].strip() == "shell-output-probe"
    finally:
        store.close()


def test_stale_approval_leaves_no_running_output_snapshot(tmp_path: Path) -> None:
    store = ExecutionOutputStore(tmp_path)
    try:
        ticket = _ticket(tmp_path, generation=1)
        result = execute_shell_ticket(_OutputContext(store), ticket)
        assert result["status"] == "stopped", result
        try:
            snapshot = store.snapshot(ticket.execution_id)
        except KeyError:
            return
        assert snapshot["execution_state"] == "unknown", snapshot
    finally:
        store.close()
