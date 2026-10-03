"""Required deterministic release contracts; failures and skips both close the gate.

Run from the repository root: python -m tools.machine_contract_gate.
This is a fixed pytest selection, not another task runner.
"""

from __future__ import annotations

import pytest

CONTRACT_TESTS = (
    "tests/test_cli.py",
    "tests/test_headless_runner.py",
    "tests/test_release_gate_tool_order.py",
    "tests/test_headless_real_kernel_lifecycle.py",
    "tests/test_event_outputs_share_run_identity.py",
    "tests/test_cli_tool_progress_uses_canonical_name.py",
    "tests/test_headless_failure_events_match_durable_lifecycle.py",
    "tests/test_event_receipt_verification_requires_exact_boolean.py",
    "tests/test_tool_event_exit_uses_authoritative_record.py",
    "tests/test_provider_connection_running_event.py",
    "tests/test_operator_auth_blocks_unauthenticated_http.py",
    "tests/test_operator_auth_rejects_non_ascii_credentials.py",
    "tests/test_entry_authorization_cannot_expand_by_route.py",
    "tests/test_entry_auth_denied_capabilities.py",
    "tests/test_local_request_rejects_unpaired_tool_history.py",
    "tests/test_native_termination_closes_all_ids.py",
    "tests/test_source_fetch_status_is_not_document_text.py",
    "tests/test_source_search_hit_targets_persist_in_canonical.py",
    "tests/test_source_lifecycle_cancel_and_deadline_propagate.py",
    "tests/test_sse_cursor_reconnect_boundaries.py",
    "tests/test_sse_browser_dedup_reset_and_buffer_gap.py",
    "tests/test_gate_diagnostics_report_physical_attempts.py",
    "tests/test_gate_diagnostics_separate_restarted_exchanges.py",
    "tests/test_local_request_diagnostics_match_wire_attempts.py",
    "tests/test_release_gate_recovery_interrupts_and_resumes.py",
    "tests/test_recovery_gate_requires_exact_checkpoint_content.py",
    "tests/test_recovery_gate_reads_utf8_receipts.py",
    "tests/test_recovery_handoff_preserves_original_task_prompt.py",
    "tests/test_settled_delivery_recovery.py",
    "tests/test_durable_recovery_preserves_failed_tool_status.py",
    "tests/test_recovery_requires_original_policy.py",
    "tests/test_recovery_restores_research_ledger.py",
)


class _RequiredChecks:
    def __init__(self) -> None:
        self.skipped: list[str] = []

    def pytest_runtest_logreport(self, report) -> None:
        if report.skipped:
            self.skipped.append(report.nodeid)

    def pytest_collectreport(self, report) -> None:
        if report.skipped:
            self.skipped.append(report.nodeid)


def run_gate() -> int:
    checks = _RequiredChecks()
    code = int(pytest.main(["-q", "-o", "faulthandler_timeout=120", *CONTRACT_TESTS], plugins=[checks]))
    if checks.skipped:
        print("Required machine checks did not run:\n" + "\n".join(checks.skipped))
        return code or 1
    return code


if __name__ == "__main__":
    raise SystemExit(run_gate())
