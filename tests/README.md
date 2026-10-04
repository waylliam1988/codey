# Test Index

Run the full deterministic suite with:

```text
pytest -q
```

## Required event and recovery contracts

`python -m tools.machine_contract_gate` runs the fixed CI/local selection;
missing checks, failures and skips fail this gate. Node.js is required.
Live release checks stay separate from deterministic contract checks.

- `test_event_outputs_share_run_identity.py`,
  `test_cli_tool_progress_uses_canonical_name.py`: run identity and strict tool
  fields agree in published events, SSE wire bytes, JSONL and CLI text.
- `test_event_receipt_verification_requires_exact_boolean.py`,
  `test_tool_event_exit_uses_authoritative_record.py`: malformed truthy status
  and invalid recorded exits cannot appear as success.
- `test_published_run_modes_are_released_at_terminal.py`: terminal publication
  releases per-run metadata even with an explicit mode.
- `test_headless_failure_events_match_durable_lifecycle.py`: connection failure,
  cancellation and delivery failure match durable state, close the connected
  provider and do not duplicate writes.
- `test_recovery_handoff_preserves_original_task_prompt.py`: append recovered
  facts to the original prompt; retain same-window native result delivery.
- `test_release_gate_recovery_interrupts_and_resumes.py`: two real child
  processes execute, crash after settlement, restore and finish through the
  formal task entry in JSON/native; the edit runs once.
- `test_recovery_gate_requires_exact_checkpoint_content.py`: matching hashes
  and passing tests cannot replace independent requested-content checks.
- `test_recovery_gate_reads_utf8_receipts.py`: UTF-8 journals remain readable
  under non-UTF-8 default locales.
- `test_gate_diagnostics_separate_restarted_exchanges.py`: recorder instances
  keep restarted exchange numbers and terminal budgets distinct.
- `test_machine_contract_gate_rejects_skipped_checks.py`: required checks
  cannot silently pass by being skipped.

## Kernel

- `test_task_kernel_remaining.py`: JSON/native parity, policy snapshots,
  hybrid/readonly behavior, completion and recovery contracts.
- `test_task_entry_cutover.py`: public task entry and end-to-end mode wiring.
- `test_task_entry_kernel_prod.py`: production entry ownership and single-loop
  architecture locks.
- `test_runtime_helper_boundaries.py`: provider, browser and ledger helper
  extraction locks.
- `test_native_delivery.py` and `test_tool_result_delivery.py`: native call-id
  closure and durable result delivery.
- `test_work_checkpoint_flow.py` and `test_recovery_ownership_regression.py`:
  interruption, resume and idempotence.

## Boundary Sweeps

- `test_operator_auth_blocks_unauthenticated_http.py`,
  `test_operator_auth_rejects_non_ascii_credentials.py`, and
  `test_operator_bootstrap_before_ui_requests.py`: real HTTP authentication,
  one-time/expired credentials and actual JavaScript boot order.
- `test_tool_execution_status_is_not_display_text.py`,
  `test_research_results_preserve_structured_status.py`,
  `test_source_fetch_status_is_not_document_text.py`, and
  `test_source_lifecycle_cancel_and_deadline_propagate.py`: explicit status and
  acquisition lifecycle exceptions.
- `test_durable_recovery_preserves_failed_tool_status.py`,
  `test_compact_slot_replay_requires_and_preserves_status.py`,
  `test_persisted_result_status_is_required.py`, and
  `test_tool_receipt_status_corruption_fails_closed.py`: real-log and compact
  replay status, unchanged failed results and corrupt receipt rejection.
- `test_context_tool_group_requires_exact_result_ids.py`,
  `test_local_request_rejects_unpaired_tool_history.py`, and
  `test_local_native_call_ids_preserve_exact_identity.py`: one-to-one IDs,
  malformed history rejected before send and history preserved on rejection.
- `test_generated_tool_schema_nested_boundaries.py`,
  `test_tool_schema_enum_distinguishes_json_booleans.py`, and
  `test_output_capture_generated_utf8_boundaries.py`: independently known
  schema witnesses and byte-slice oracles, with fixed reproducible seeds.
- `test_local_request_diagnostics_match_wire_attempts.py` and
  `test_gate_diagnostics_report_physical_attempts.py`: actual request bytes,
  retry accounting and diagnostic failure isolation.
- `test_sse_cursor_reconnect_boundaries.py`,
  `test_sse_browser_dedup_reset_and_buffer_gap.py`,
  `test_headless_tool_status_requires_exact_boolean.py`, and
  `test_tool_result_ui_headless_semantic_parity.py`: cursor gaps/restarts,
  actual JavaScript deduplication and the common tool-event projection.

- `test_coldstart_provider_trace_cleanup.py`: provider and trace dead fields.
- `test_coldstart_contract_cleanup.py`: strict payload and projection contracts.
- `test_coldstart_export_cleanup.py`: package export and shared helper cleanup.
- `test_fail_closed_numeric_guards.py`: numeric and hostname input rejection.
- `test_fail_closed_boundary_sweep.py`: schema, host, API and ledger guards.
- `test_fail_closed_runtime_sweep.py`: runtime fail-closed regression sweep.

## Research Experiments

The important live A/B probes remain under `tests/manual/` and use the shared
`run_research_iteration` function through the shared kernel. Some comparison
fixtures use the test-only `tests/support/research_iteration_adapter.py` class;
there is no production `ResearchIteration` class:

- `deep_research_core_ab.py`
- `research_repair_prompt_ab.py`
- `concept_context_ab.py`
- `research_source_rendering_ab.py`

Their deterministic pytest wrappers are `test_deep_research_core_ab.py`,
`test_research.py`, `test_research_completion_gate.py`, and
`test_research_pipeline.py`. Historical result JSON files remain readable by
the A/B journal tools; they are data artifacts, not production compatibility
modules.

Research-only codec/controller fixtures and the legacy iterator adapter live in
`tests/support/` so production code has one task protocol, one task loop, and
one research entry.

## Desktop / CLI task parity

- `test_desktop_cli_project_review_parity.py`: actual desktop submission and
  actual CLI command, automatic approval/finding and single repair, matching
  tool facts and one terminal; shared non-Git local fixture is in `support/`.
- `test_cli_task_intents_and_authorization.py`: intents, grants, sessions,
  reuse, incompatible options and human review progress.
- `test_headless_desktop_entry_authorization_parity.py` and
  `test_headless_shared_services_preserve_policy_boundaries.py`: common entry
  grants/requirements, no-project restrictions, require-web and bad configuration.
- `test_shared_task_service_consumers.py`: all four app service callbacks
  receive the correct state and policy through both formal entries.
- `test_cli_review_readonly_and_cold_reuse.py`: two independently closed CLI
  contexts restore a persisted review without a new chat/send or file writes.
- `test_entry_authorization_requirement_errors_propagate.py`: failed
  derivation cannot silently remove required modifications.
- `test_operator_http_fixture_sends_complete_request.py`: deterministic early
  peer rejection locks the auth fixture's complete-request write; no retries.
- `no_external_advisor_models` is an opt-in fixture for scripted entry tests;
  real service selection remains active, while user model discovery is isolated.

## Live Gates

- Automatic local review contracts (also required by `machine_contract_gate`):
  `test_project_auto_local_review_lifecycle.py` uses the formal project entry
  with both injected and no-web desktop review routing, approval/finding,
  unknown reply and default shared desktop/headless review;
  `test_review_writer_reentry_requires_new_attempt.py` locks durable reentry;
  `test_review_line_endings_do_not_mark_redaction.py` and
  `test_review_redaction_preserves_declared_symbols.py` distinguish normal
  code/normalization from secret redaction;
  `test_project_review_gate_requires_real_automatic_flow.py` rejects missing,
  reordered or conflicting gate facts. These are not model quality A/B tests.
- `tools/local_model_release_gate.py`: release-blocking local OpenAI-compatible
  model gate. It covers chat/read, create/edit/references, the shared `hybrid`
  entry, read-only planning, discussion, auto routing, test generation, strict
  Research, recovery, Ghost state, read-only review and automatic `project_review`.
  The latter uses one run rather than submitting a second review task. Each
  attempt has a process deadline and a unique artifact directory. Objective
  completion, independently correct artifacts, conversation safety and Ghost
  control-plane checks are reported separately; answer quality is not automated.
- `tools/local_model_diagnostic_probe.py`: non-blocking local-model diagnostic
  probe for hostile fixtures, work queues, search, and shell/reporting edges.
- `tests/multi_model_snake_smoke.py`: browser-provider multi-model smoke for
  discussion, project creation, independent verification, review, audit, and
  follow-up repair. It is separate from the local-model release gate.
