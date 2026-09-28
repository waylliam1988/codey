# Test Index

Run the full deterministic suite with:

```text
pytest -q
```

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

- `test_coldstart_provider_trace_cleanup.py`: provider and trace dead fields.
- `test_coldstart_contract_cleanup.py`: strict payload and projection contracts.
- `test_coldstart_export_cleanup.py`: package export and shared helper cleanup.
- `test_fail_closed_numeric_guards.py`: numeric and hostname input rejection.
- `test_fail_closed_boundary_sweep.py`: schema, host, API and ledger guards.
- `test_fail_closed_runtime_sweep.py`: runtime fail-closed regression sweep.

## Research Experiments

The important live A/B probes remain under `tests/manual/` and use the shared
`ResearchIteration` entry:

- `deep_research_core_ab.py`
- `research_repair_prompt_ab.py`
- `concept_context_ab.py`
- `research_source_rendering_ab.py`

Their deterministic pytest wrappers are `test_deep_research_core_ab.py`,
`test_research.py`, `test_research_completion_gate.py`, and
`test_research_pipeline.py`. Historical result JSON files remain readable by
the A/B journal tools; they are data artifacts, not production compatibility
modules.

Research-only codec/controller fixtures live in `tests/support/` so production
code has one task protocol and one task loop.

## Live Gates

- `tools/local_model_release_gate.py`: release-blocking local OpenAI-compatible
  model gate. It covers chat, create/edit/references, the shared `hybrid`
  entry, read-only planning, discussion, auto routing, and Ghost state.
- `tools/local_model_diagnostic_probe.py`: non-blocking local-model diagnostic
  probe for hostile fixtures, work queues, search, and shell/reporting edges.
- `tests/multi_model_snake_smoke.py`: browser-provider multi-model smoke for
  discussion, project creation, independent verification, review, audit, and
  follow-up repair. It is separate from the local-model release gate.
