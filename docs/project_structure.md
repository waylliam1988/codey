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
  providers/    Browser adapters, shared API runtime, protocol codecs, connection packages and health
  protocols/    Shared JSON/native plan codecs and framing
  toolchain/    Shared tool specifications, schema checks and project tool implementations
  completion/   Completion contracts, engine, verification and edit integrity
  research/     Sources, evidence, report checks, pipeline strategies and records
  runtime/      Durable operation state, facts, effects, writes and read-only observations
  workspace/    Project paths, revisions, maps, changes, facts and context
  storage/      Atomic I/O, file locks, conversation and managed-output storage
  knowledge/    Local notes, graph and knowledge changes
  ghost/        Bounded local experience, memory, work queues and control surfaces
  reviews/      Review contract, safe input, identity, artifacts, explicit reuse and coordination
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
HTTP / CLI -> shared task services + entry authorization
           -> task submission -> task_run + task_phases
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
| `codey/app/task_services.py`, `task/entry_auth.py` | Compose desktop/headless review and advisor services once; derive HTTP and CLI authorization with one rule set |
| `codey/operations/task_state.py` | `TaskState` and the typed submission-store bundle |
| `codey/operations/task_run.py`, `task_phases/` | Run resource lifecycle, provider setup, callbacks and terminal settlement |
| `codey/operations/task_entry.py`, `task_session.py` | Entry policy and per-task facts |
| `codey/operations/task_loop.py` | One production model/tool loop; typed transport/execution/observation dependencies |
| `codey/operations/task_guidance.py`, `research/completion_guidance.py` | Task composition selects domain-owned completion guidance; the kernel receives text, not a Research workflow |
| `codey/operations/project_prompt_context.py`, `workspace/coding_context.py` | Prepare immutable coding facts with the existing verification decision, then render JSON/native context without I/O |
| `codey/operations/kernel_prompt.py`, `research/tool_contract.py` | Compose supplied guidance/context and protocol instructions; logical note-ID usage belongs to the shared tool definition |
| `codey/toolchain/tool_spec.py` | Tool definitions, schemas and permission-aware turn snapshots |
| `codey/operations/kernel_protocol.py` | JSON/native normalization and validation against the current snapshot |
| `codey/operations/kernel_execution.py`, `task_execution.py` | Execution boundary and domain adapters |
| `codey/operations/completion_gate.py` | Final completion-proof composition; a model's `done` is only a proposal |
| `codey/operations/project_completion_checks.py`, `research_completion_checks.py` | Project and source/strict-Research check providers |
| `codey/operations/kernel_session_recovery.py`, `kernel_receipts.py` | Restore original policy/facts and settled results; validate receipt identity |
| `codey/providers/local_response_codec.py` | Local response envelopes and model dialects, before the kernel |
| `codey/providers/base.py` | Protocol-neutral tool definitions/results and explicit assistant finish state |
| `codey/providers/api_provider.py`, `api_transport.py` | Shared generation lock, candidate commit, cancellation, deadline and one bounded POST; unknown outcomes are never replayed |
| `codey/providers/api_chat.py`, `api_responses.py` | Protocol-owned tools, results, wire history, complete-exchange compaction and reply decoding; Responses replays reasoning items and uses call_id |
| `codey/providers/api_connections.py`, `local_connection.py` | Lazy connection factories and admitted Local configuration; shared runtime does not import Zen |
| `codey/providers/zen/` | Public/free catalog, scoped partner identity and access observations; `declarations.py` owns the temporary unavailable read/shell wire profile, and `connection.py` owns bounded text-call rejection. No task grants; removable with its API_CONNECTIONS registration |
| `codey/runtime/core/api_selection.py`, `operation_payload.py` | Non-secret frozen API selection and strict admission/delivery payload validation |
| `codey/research/source_gateway.py`, `tools.py` | Explicit acquisition/tool outcomes; cancellation/deadlines propagate without fallback acquisition |
| `codey/app/context.py`, `event_bus.py`, `event_payloads.py` | Common run identity/mode and strict status publication; replay bus and pure bounded machine-event/receipt projection |
| `codey/app/headless_runner.py`, `cli.py`, `web/assets/sse.js` | Consume common events as JSONL, CLI text and browser reconciliation without inferring success |
| `tools/machine_contract_gate.py` | Shared CI/local required checks; missing tests, failures and skips close the gate |
| `tools/local_model_gate_recovery.py` | Gate-only process interruption and independent recovery verification using the formal entry, with no model loop of its own |
| `codey/operations/research_iteration.py` | `run_research_iteration`, the pipeline's shared-kernel adapter |

## Durable runtime and storage

### Review ownership

| Owner | Consumer / boundary |
| --- | --- |
| `reviews/core.py`, `findings.py` | Bounded reply parsing, actionable findings and common review event metadata |
| `reviews/input.py`, `identity.py` | Actual safe prompt/scope, API model/settings identity, bounded file and Git-basis snapshot checks |
| `reviews/persistence.py`, `reuse.py` | One verified artifact read, finished-ledger lineage and explicit same-session/project reuse |
| `app/review_service.py` | Single input preparation, actual Reviewer selection, one format repair, persistence; unknown send failures are not retried through another Reviewer |
| `reviews/coordinator.py`, `operations/project_review_phase.py` | Current-snapshot findings may enter the existing single Writer repair |
| `operations/review_flow.py`, `app/headless_runner.py` | Shared review service: read-only review and default automatic project review on desktop and CLI/headless; optional pinned Reviewer connector for gates |
| `runs/ledger_projection.py`, `runs/details.py`, `app/api.py` | Consistent bounded display plus authenticated `/api/run_review` cold structured reads |
| `tools/local_model_gate_review.py` | Real Reviewer request, unchanged files, verified artifact/ledger/event consistency; not model finding-quality scoring |
| `tools/local_model_gate_project_review.py` | One formal project run: code/check, automatic isolated local review, optional existing repair; independent file/test and real request checks |

Review repair re-enters `writer_running` with a newer attempt only after a
successful settled Writer and before the final proof; it settles that attempt
before completion enforcement. It does not create a second tool loop or reuse
a previous settled verdict as permission to execute effects.

Model identity describes configured target/model/settings, not immutable model
weights. Snapshot inventories and file reads have finite bounds; scan failures
or exhausted bounds decline trust. These are explicit limits, not a whole-project
no-bug proof or a guarantee against arbitrary concurrent writes.

API project Writers admit an independent model from the same connection when
available; standalone read-only review uses its selected model. Reviewer choice
is frozen in the formal operation log before sending. Standalone API review pins
the selected model before considering open websites. Project review's web preference,
require-web policy, self-review policy and one-repair limit remain authoritative.
Plain access observations are short-lived service facts, not permanent capability
promises; an unknown request never causes another-model replay.

The shared API runtime has no Zen branches. Local alone opts into its text-frame
decoder; Responses rejects explicitly unfinished output items before committing
history. See [Zen request profile](zen_request_profile_2026-10-08.md).

`runtime/core` owns state/contracts, `runtime/log` canonical log projections,
`runtime/effects` effect/result delivery, `runtime/write` admitted mutations,
and `runtime/observe` read-only observations. The runtime layer does not depend
on task orchestration. See [runtime architecture](runtime_architecture.zh-CN.md).

A fresh provider window retains the original task prompt and appends settled
facts; a retained native window receives results with their original IDs.
Task facts are reconstructed from original log/receipts. Research evidence is a
domain projection, not a competing task log. Managed outputs and recovery
receipts may retain source text/tool output locally; audit summaries and model
windows are bounded independently.

Tool success is explicit through settlement and recovery, including failed
results. Receipt status must agree with the settlement. Research fetch adapters
return `status` (`ok/error/skipped`), plus a failure `detail` when applicable;
`ResearchToolOutput.ok` stays explicit until it becomes the common `ToolResult`.
Native history requires one result per unique call ID in each tool group.

Admission records `model_selection` and, when used, `reviewer_selection` in the
canonical operation state: connection revision, model, protocol, capabilities
and generation settings, without credentials. Cold restart reuses this choice;
changed Settings do not replace it, and an unavailable connection blocks recovery.
Protocol history is in-memory and protocol-owned. Without reusable history,
recovery creates a fresh window with original task requirements and settled facts,
never repeating a settled write. `final_delivery` records success/failed/unknown
separately from completion proof; delivery failure does not erase completed effects.

The manual gate recorder observes bytes at the local provider's request
boundary. Request hashes diagnose changes; they grant no permission and prove
neither semantic equivalence nor completion. It distinguishes logical exchanges
from the single bounded HTTP attempt. HTTP uncertainty, bounded model continuation
after an explicit output limit, and task recovery are separate mechanisms.

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
