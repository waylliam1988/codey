# Project structure and ownership

[中文](project_structure.zh-CN.md)

This is the current source map. Historical design plans and experiment reports
describe their recorded revisions; they are not the current API reference.

```text
codey/
  __init__.py   Single source of the package version
  __main__.py   python -m codey entry
  app/          HTTP/desktop/CLI entry, registries, task submission and resource ownership
  automation/   Local automation jobs and scheduling
  task/         User submission and task-kind data
  policies/     Task grants, action guards, network and command boundaries
  operations/   Task entry, shared kernel, execution adapters, completion and recovery wiring
  agents/       Project request/result, prompts, context, review and approval helpers
  providers/    Browser/local adapters, response codecs, sessions, worker and health
  protocols/    Shared JSON/native plan codecs and framing
  toolchain/    Shared tool specifications, schema checks and project tool implementations
  completion/   Completion contracts, engine, verification and edit integrity
  research/     Sources, evidence, report checks, pipeline strategies and records
  runtime/      Durable operation state, facts, effects, writes and read-only observations
  workspace/    Project paths, revisions, maps, changes, facts and context
  storage/      Atomic I/O, file locks, conversation and managed-output storage
  knowledge/    Local notes, graph and knowledge changes
  ghost/        Bounded local experience, memory, work queues and control surfaces
  reviews/      Review coordination and findings
  repairs/      Provider repair jobs and supervision
  runs/         Run ledgers, traces, checkpoints, details and receipts
  utils/        Small shared text, reference and scan helpers
  web/          Packaged local UI and JavaScript/CSS assets
tests/          Behavioral regressions, architecture locks, stress and local/browser E2E
  support/      Test-only fixtures and historical comparison adapters
  manual/       Live experiments and their historical reports
tools/          Release gates, diagnostics, parity and development utilities
docs/           Current architecture, release guidance and dated audit reports
```

## Follow a task

```text
HTTP / CLI -> task submission -> task_run + task_phases
           -> task_entry -> TaskPolicy + TaskSession + KernelRunRequest
           -> task_loop.run_task_kernel
                -> TurnSnapshot -> provider reply -> normalized tool plan
                -> authorization -> execute_turn -> intent / result settlement
                -> provider result delivery -> completion_gate -> final result
```

Project, Research, planning and authorized mixed tasks share the model/tool
kernel. Domain strategies can schedule follow-up work; they do not own another
model/tool loop. The Research button selects strict evidence/report requirements.
Using web tools in ordinary coding does not automatically require research notes.

| Owner | Responsibility |
| --- | --- |
| `codey/app/operator_auth.py`, `web/assets/operator_auth.js` | Process-local HTTP/SSE operator credential and UI bootstrap; independent of model/task grants |
| `codey/runtime/core/models.py` | Immutable `ToolResult` with mandatory exact boolean status; display text has no status authority |
| `codey/operations/task_state.py` | `TaskState` and the typed submission-store bundle |
| `codey/operations/task_run.py`, `task_phases/` | Run resource lifecycle, provider setup, callbacks and terminal settlement |
| `codey/operations/task_entry.py`, `task_session.py` | Entry policy and per-task facts |
| `codey/operations/task_loop.py` | One production model/tool loop; typed transport/execution/observation dependencies |
| `codey/toolchain/tool_spec.py` | Tool definitions, schemas and permission-aware turn snapshots |
| `codey/operations/kernel_protocol.py` | JSON/native normalization and validation against the current snapshot |
| `codey/operations/kernel_execution.py`, `task_execution.py` | Execution boundary and domain adapters |
| `codey/operations/completion_gate.py` | Final completion-proof composition; a model's `done` is only a proposal |
| `codey/operations/project_completion_checks.py`, `research_completion_checks.py` | Project and source/strict-Research check providers |
| `codey/operations/kernel_session_recovery.py`, `kernel_receipts.py` | Restore original policy/facts and settled results; validate receipt identity |
| `codey/providers/local_response_codec.py` | Local response envelopes and model dialects, before the kernel |
| `codey/agents/context_compaction.py`, `providers/local_openai.py` | Exact native history pairing before compaction/request; actual serialized-request diagnostic hook |
| `codey/research/source_gateway.py`, `tools.py` | Explicit acquisition/tool outcomes; cancellation/deadlines propagate without fallback acquisition |
| `codey/app/event_bus.py`, `web/assets/sse.js`, `app/headless_runner.py` | Replay cursors, reconciliation and consistent structured event projections |
| `codey/operations/research_iteration.py` | `run_research_iteration`, the pipeline's shared-kernel adapter |

## Durable runtime and storage

`runtime/core` owns state/contracts, `runtime/log` canonical log projections,
`runtime/effects` effect/result delivery, `runtime/write` admitted mutations,
and `runtime/observe` read-only observations. The runtime layer does not depend
on task orchestration. See [runtime architecture](runtime_architecture.zh-CN.md).

Task facts are reconstructed from original log/receipts. Research evidence is a
domain projection, not a competing task log. Managed outputs and recovery
receipts may retain source text/tool output locally; audit summaries and model
windows are bounded independently.

Tool success is explicit through settlement and recovery, including failed
results. Receipt status must agree with the settlement. Research fetch adapters
return `status` (`ok/error/skipped`), plus a failure `detail` when applicable;
`ResearchToolOutput.ok` stays explicit until it becomes the common `ToolResult`.
Native history requires one result per unique call ID in each tool group.

The manual gate recorder observes bytes at the local provider's request
boundary. Request hashes diagnose changes; they grant no permission and prove
neither semantic equivalence nor completion. It distinguishes logical exchanges
from the explicit `urlopen` attempts, including the existing transport retries.

Ghost work queues and affinity each separate `*_model`, `*_sources`,
`*_events` and the store module. These are domain owners, not generic framework
layers; continuity and Hebbian storage keep their existing coherent modules.

## Where to verify a change

- [Test index](../tests/README.md): behavioral and ownership regressions.
- [Release gate](release_gate.zh-CN.md): static, deterministic, machine and live-model checks.
- [Kernel invariants](kernel_invariants.zh-CN.md): executable checks and their finite proof scope.
- [Test report](../TEST_REPORT.md): actual results and environment exclusions.

The old coding/research loops and production `ResearchIteration` class are
deleted. A comparison adapter with that name exists only under `tests/support/`.
Current consumers use the function adapter and owning modules; historical
fixtures are not a production compatibility promise.
