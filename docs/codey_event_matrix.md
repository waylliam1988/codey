# Codey Event / Capability Matrix v1

This matrix is an architecture ledger for Codey's built-in events and event
projections. It is documentation plus tests, not runtime configuration. It does
not create an event bus, dispatch work, alter routing, or change UI/SSE payload
shapes.

`model_visible=true` means the event projection can affect text sent to a web
model. Those rows must be covered by Prompt Envelope and Run Trace. `ui_visible`
uses `false`, `sse:<surface>`, or a known UI surface name. `durable_state` uses
the stable capability durable-state names or `none`.

## Matrix Hygiene

Rows below describe implemented event/projection surfaces, not future intent.
Roadmap-only concepts such as World Model ContextSource, Research untrusted
source wrapper, or provider protocol outcome learning should be added here only
after code emits or consumes a real event/projection. Until then, their
invariants live in the roadmap and principle documents.

Adding a model-visible row requires the implementation to prove:

```text
Prompt Envelope source refs exist
Run Trace records digest/counts/refs, not raw prompt or raw source body
privacy_boundary says whether the content is data, hint, proof, or UI only
no row can turn Ghost / World Model / transcript / source body into evidence
```

## Capability Vocabulary

Valid `capability_id` stamps are documentation-owned architecture boundaries,
not runtime dispatch objects.

- `agent_runner`
- `changes_presenter`
- `chat_runner`
- `completion_contract`
- `completion_repair_context`
- `consensus_advisors`
- `context_epoch`
- `conversation_handoff`
- `domain_evidence_profiles`
- `local_context`
- `permission_profile_catalog`
- `policy_guard`
- `prompt_envelope`
- `provider_capability_registry`
- `provider_factory`
- `research_brief_projection`
- `research_connector_search`
- `research_evidence_ledger`
- `research_evidence_runtime`
- `research_object_model`
- `research_proof_quality`
- `research_query_planner`
- `research_review_finding`
- `research_runner`
- `research_source_connectors`
- `research_source_trust`
- `research_topic_continuity`
- `review_runner`
- `run_details`
- `run_ledger`
- `runtime_operations`
- `run_trace`
- `tool_runtime`

Policy-gated capabilities:

- `agent_runner`
- `local_context`
- `provider_factory`
- `research_connector_search`
- `research_runner`
- `research_source_connectors`
- `review_runner`
- `tool_runtime`

Durable state vocabulary:

- `change_snapshots`
- `local_context`
- `managed_outputs`
- `project_facts`
- `provider_controls`
- `provider_health`
- `research_evidence_ledger`
- `research_notes`
- `research_provenance`
- `run_ledger`
- `runtime_session_log`
- `run_trace`
- `work_checkpoints`

UI surface vocabulary:

- `changes_drawer`
- `chat_stream`
- `composer`
- `local_context_drawer`
- `research_drawer`

| event_id | producer | consumers | capability | durable_state | model_visible | ui_visible | policy_required | trace_required | privacy_boundary |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| run_event.turn | codey.runtime.events.RunEvent.turn_started | task_run, run_ledger, sse:chat_stream | agent_runner | run_ledger | false | sse:chat_stream | false | false | UI/SSE and ledger projection only; Review model-visible recent log is declared separately |
| run_event.tool_start | codey.runtime.events.RunEvent.tool_started | task_run, run_ledger, sse:chat_stream | tool_runtime | run_ledger | false | sse:chat_stream | true | false | tool name, display path, activity, and optional command projection only |
| run_event.tool | codey.runtime.events.RunEvent.tool_finished | task_run, run_ledger, sse:chat_stream | tool_runtime | run_ledger | false | sse:chat_stream | true | false | presentation result, status, booleans, exit code, and managed-output metadata only |
| run_event.info | codey.runtime.events.RunEvent.info | task_run, run_ledger, sse:chat_stream | agent_runner | run_ledger | false | sse:chat_stream | false | false | bounded status text and optional provider names |
| run_event.status | codey.runtime.events.RunEvent.status | task_run, run_ledger | agent_runner | run_ledger | false | false | false | false | bounded local progress text only |
| sse.run_event_ui_payload | codey.runtime.events.run_event_ui_payload | task_run, sse:chat_stream | agent_runner | none | false | sse:chat_stream | false | false | existing UI/SSE payload projection only; no extra durable copy |
| run_ledger.model_reply | codey.agents.runner | run_ledger, receipt_projection | run_ledger | run_ledger | false | false | false | false | bounded model reply record for task receipt projection |
| run_ledger.tool_started | codey.operations.task_run | run_ledger, receipt_projection | run_ledger | run_ledger | false | false | true | false | action_policy-checked tool-start projection without source bodies or long command output |
| run_ledger.tool_finished | codey.operations.task_run | run_ledger, receipt_projection | run_ledger | run_ledger | false | false | true | false | action_policy-checked presentation and audit metadata; managed output handle only |
| run_ledger.provider_failure | codey.operations.task_run | run_ledger, receipt_projection | run_ledger | run_ledger | false | false | false | false | provider id, phase, bounded reason code, and recovery metadata |
| run_ledger.changes_collected | codey.operations.project_completion_flow | run_ledger, changes_drawer, receipt_projection | changes_presenter | run_ledger | false | changes_drawer | false | false | file paths, counts, and diff summary metadata only |
| run_details.summary | codey.runs.details | server, chat_stream | run_details | none | false | chat_stream | false | false | bounded user-facing run explanation derived from ledger, trace, and runtime operation metadata only |
| runtime_operation.state | codey.runtime.operation_state | task_run, run_details | runtime_operations | runtime_session_log | false | false | false | false | leaf, driver, pending effect ids, delivery batch ref, provider id, turn counts, proof refs/status, repair round count, repair-context digest, and bounded blocked reason only; prompts, replies, stdout, diffs, and source bodies are excluded |
| run_trace.prompt_sections | codey.runtime.prompt_envelope | run_trace, prompt_envelope | prompt_envelope | run_trace | false | false | false | true | section names, source refs, lengths, truncation flags, and hashes only |
| run_trace.policy_decisions | codey.policies.action | run_trace, policy_guard | policy_guard | run_trace | false | false | true | true | decision, guard id, reason code, phase, subject ref, and display digest |
| run_trace.fallbacks | codey.operations.task_run | run_trace, provider_factory | provider_factory | run_trace | false | false | true | true | provider ids, phases, bounded reason codes, and recovery outcome |
| run_trace.provider_failures | codey.operations.task_run | run_trace, provider_factory | provider_factory | run_trace | false | false | false | true | provider id, phase, bounded error class, and retry/fallback metadata |
| review.recent_log | codey.runtime.events.render_run_event | review_runner, prompt_envelope, run_trace | review_runner | none | true | false | true | true | bounded recent run log rendered for Review prompt, including writer reply and tool summaries from the current run |
| tool_outcome.model_text | codey.toolchain.runtime | agent_runner, prompt_envelope, run_trace | tool_runtime | none | true | false | true | true | bounded model-facing tool text only; presentation, audit, and canonical projections excluded |
| tool_outcome.presentation | codey.toolchain.runtime | codey.runtime.events, task_run, sse:chat_stream, run_ledger | tool_runtime | none | false | sse:chat_stream | true | false | UI-facing status/result fields with managed-output handle metadata only |
| tool_outcome.audit | codey.toolchain.runtime | run_ledger, run_trace | tool_runtime | run_trace | false | false | true | true | small JSON-safe audit facts, exit code, truncation facts, and managed-output metadata |
| tool_outcome.canonical | codey.toolchain.runtime | agent_runner, tests | tool_runtime | none | false | false | true | false | small JSON-safe internal facts only |
| research.notes | codey.research.runner | research_drawer, run_ledger | research_runner | research_notes | false | research_drawer | true | false | note ids, titles, bounded snippets, and provenance refs |
| research.sources | codey.research.runner | research_drawer, run_ledger | research_runner | research_provenance | false | research_drawer | true | false | source ids, URLs after URL policy, titles, and bounded extraction summaries |
| research.synthesis | codey.research.runner | agent_runner, prompt_envelope, run_trace, research_drawer | research_runner | research_notes | true | research_drawer | true | true | bounded synthesis text and source refs; fetched bodies are not persisted here |
| research.object_model | codey.research.object_model | research_runner, run_trace | research_object_model | run_trace | false | false | false | true | record id, answer status, source/evidence/claim counts, assumption counts, and record digest only in trace; bounded record remains internal |
| research.evidence_ledger | codey.research.evidence_ledger | research_flow, run_trace, proof_quality | research_evidence_ledger | research_evidence_ledger, run_trace | false | false | false | true | local ledger ref, record id, object counts, locator refs, hashes, and bounded excerpts only; prompts, provider errors, full pages, source text, and secret-looking reason/warning codes are excluded |
| research.proof_quality | codey.research.proof_quality | research_flow, run_trace, ghost.work_queue | research_proof_quality | run_trace | false | false | false | true | proof ref, queued-question digest, pass/fail booleans, counts, trace-safe reason codes, and optional record id/digest for failed reviews only; passing reviews require record id/digest; follow-up text, query text, prompts, pages, URLs, source text, and malformed list scalars are excluded |
| research.source_connectors | codey.research.source_connectors | query_planner, tests | research_source_connectors | none | false | false | true | false | canonical non-sensitive connector ids/kinds, status, source refs, hit refs, digests, trace-safe source quality hints, failure modes, warnings, and errors only; source hit records are locator candidates, not evidence, and hit/fetched scalar audit fields are allow-listed |
| research.query_plan | codey.research.query_planner | research_flow, run_trace | research_query_planner | run_trace | false | false | false | true | bounded follow-up plan ref, proof ref, question digest, query count, source preference ids, max bounds, warnings, and reason codes only; query text, prompts, pages, URLs, paths, and source text are excluded |
| local_context.refs | codey.workspace.context_source | agent_runner, prompt_envelope, run_trace, local_context_drawer | local_context | local_context | true | local_context_drawer | true | true | bounded file refs, symbols, ranges, and hashes rather than source text dumps |
| local_context.actions | codey.operations.prompting | local_context_drawer, policy_guard, run_trace | local_context | local_context | false | local_context_drawer | true | true | action kind, path refs, decision metadata, and bounded display digest |
| ghost.continuity | codey.ghost.continuity | agent_runner, prompt_envelope, run_trace | local_context | local_context | true | false | true | true | bounded continuity facts, scopes, and refs only |
| ghost.directive | codey.ghost.directive | agent_runner, prompt_envelope, run_trace | local_context | local_context | true | false | true | true | bounded local directive text generated from approved Ghost state |
| ghost.work_queue | codey.ghost.work_queue | agent_runner, prompt_envelope, run_trace | local_context | work_checkpoints | true | false | true | true | bounded item ids, status, proof refs, task summaries, observed-source refs, transition preconditions, and snapshot anchors |
| ghost.affinity_events | codey.ghost.affinity | local_context, run_trace | local_context | local_context | false | false | true | true | bounded reinforcement specs, scope ids, weights, event ids, provenance refs, and snapshot anchors |
| provider.fallback | codey.operations.task_run | run_trace, run_ledger, policy_guard | provider_factory | provider_health | false | false | true | true | from/to provider ids, phase, reason code, and recovery decision |
| provider.recovery | codey.providers.revival | run_trace, run_ledger, provider_factory | provider_factory | provider_health | false | false | true | true | provider id, bounded health status, and retry metadata |
| managed_output.artifact | codey.storage.managed_outputs | run_ledger, run_trace, policy_guard | tool_runtime | managed_outputs | false | false | true | true | artifact handle, byte counts, hash, and truncation metadata; stored file remains local |
| changes.diff_presentation | codey.workspace.changes | changes_drawer, run_ledger | changes_presenter | change_snapshots | false | changes_drawer | false | false | changed paths, counts, and bounded diff presentation metadata |
| changes.snapshots | codey.workspace.changes | changes_drawer, run_ledger, policy_guard | changes_presenter | change_snapshots | false | changes_drawer | true | false | snapshot ids, paths, counts, and restore/action decision metadata |
