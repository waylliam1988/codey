"""Required deterministic release contracts; failures and skips both close the gate.

Run from the repository root: python -m tools.machine_contract_gate.
This is a fixed pytest selection, not another task runner.
"""

from __future__ import annotations

import pytest

CONTRACT_TESTS = (
    "tests/test_cli.py",
    "tests/test_desktop_cli_project_review_parity.py",
    "tests/test_cli_task_intents_and_authorization.py",
    "tests/test_headless_desktop_entry_authorization_parity.py",
    "tests/test_shared_task_service_consumers.py",
    "tests/test_cli_review_readonly_and_cold_reuse.py",
    "tests/test_windows_spawn_assigns_job_before_resuming_process.py",
    "tests/test_headless_shared_services_preserve_policy_boundaries.py",
    "tests/test_entry_authorization_requirement_errors_propagate.py",
    "tests/test_kernel_prompt_task_guidance_ownership.py",
    "tests/test_kernel_prompt_rendering_has_no_side_effects.py",
    "tests/test_native_coding_context_uses_native_instructions.py",
    "tests/test_task_guidance_reaches_production_entries.py",
    "tests/test_prepared_coding_context_is_immutable.py",
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
    "tests/test_desktop_boot_connection_burst_fits_accept_queue.py",
    "tests/test_operator_http_fixture_sends_complete_request.py",
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
    "tests/test_api_generation_observations_no_replay.py",
    "tests/test_api_exchange_lifecycle_consistency.py",
    "tests/test_native_auto_initial_turn_prepared_once.py",
    "tests/test_optional_zen_removal_preserves_local_api.py",
    "tests/test_ui_workflow_readiness_without_animation_frames.py",
    "tests/test_provider_neutral_tool_boundaries.py",
    "tests/test_api_selection_cold_start_persistence.py",
    "tests/test_api_transport_response_uncertainty.py",
    "tests/test_api_transport_tls_handshake_retries_only_before_http_submission.py",
    "tests/test_native_auto_first_turn_preserves_tools_budget_and_authorization.py",
    "tests/test_native_cancel_and_budget_close_preserve_authorized_tool_declarations.py",
    "tests/test_entry_authorization_preserving_tests_is_not_global_readonly.py",
    "tests/test_live_api_gates_use_selected_connection_and_protocol.py",
    "tests/test_ui_gate_direct_script_resolves_sibling_recorders.py",
    "tests/test_research_provenance_python_path_api_is_not_an_unopened_source.py",
    "tests/test_research_provenance_stdlib_path_calls_are_not_source_domains.py",
    "tests/test_research_page_title_excludes_svg_accessibility_labels.py",
    "tests/test_research_provenance_path_constructor_literals_are_not_domains.py",
    "tests/test_api_generation_deadline_interrupts_partial_frame.py",
    "tests/test_api_stream_terminal_and_cancelled_generation.py",
    "tests/test_responses_protocol_tool_history_and_stream_completion.py",
    "tests/test_web_chat_responses_share_authorization_and_completion.py",
    "tests/test_terminal_delivery_preserves_completion_proof.py",
    "tests/test_terminal_tools_are_previously_declared.py",
    "tests/test_api_reviewer_uses_distinct_model_and_identity.py",
    "tests/test_standalone_api_review_keeps_explicit_model.py",
    "tests/test_zen_transport_declarations_never_grant_execution.py",
    "tests/test_custom_tool_fixture_restores_registry.py",
    "tests/test_live_zen_gate_fingerprints_connection_adapter.py",
    "tests/test_shared_api_text_decoder_requires_connection_opt_in.py",
    "tests/test_responses_incomplete_items_do_not_commit_or_execute.py",
    "tests/test_api_rejection_messages_preserve_status_and_service_reason.py",
    "tests/test_api_http_refusal_is_not_transient_provider_failure.py",
    "tests/test_run_details_do_not_invent_review_on_api_failure.py",
    "tests/test_scripted_entry_admission_and_store_cleanup.py",
    "tests/test_zen_catalog_free_protocols_and_scoped_identity.py",
    "tests/test_zen_review_access_observations_expire_without_replay.py",
    "tests/test_zen_readonly_refusal_preserves_permissions_without_replay.py",
    "tests/test_api_connection_removal_and_ui_model_selection.py",
    "tests/test_release_gate_recovery_interrupts_and_resumes.py",
    "tests/test_recovery_gate_requires_exact_checkpoint_content.py",
    "tests/test_recovery_gate_reads_utf8_receipts.py",
    "tests/test_recovery_handoff_preserves_original_task_prompt.py",
    "tests/test_settled_delivery_recovery.py",
    "tests/test_durable_recovery_preserves_failed_tool_status.py",
    "tests/test_recovery_requires_original_policy.py",
    "tests/test_recovery_restores_research_ledger.py",
    "tests/test_review_contract_integration.py",
    "tests/test_review_reuse_integration.py",
    "tests/test_headless_review_uses_selected_provider.py",
    "tests/test_review_history_http_and_reuse_lineage.py",
    "tests/test_review_artifact_strict_read_and_terminal_conflicts.py",
    "tests/test_review_consumer_and_provider_failure_boundaries.py",
    "tests/test_review_http_rejections_preserve_known_failure_without_replay.py",
    "tests/test_review_parser_preserves_incomplete_results.py",
    "tests/test_review_submission_rejects_coerced_source_ids.py",
    "tests/test_local_review_gate_rejects_unavailable.py",
    "tests/test_local_review_gate_cleans_readonly_git_objects.py",
    "tests/test_project_auto_local_review_lifecycle.py",
    "tests/test_review_line_endings_do_not_mark_redaction.py",
    "tests/test_review_redaction_preserves_declared_symbols.py",
    "tests/test_review_writer_reentry_requires_new_attempt.py",
    "tests/test_project_review_gate_requires_real_automatic_flow.py",
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
