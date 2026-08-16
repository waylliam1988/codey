# Codey Event / Capability Matrix v1

This matrix is an architecture ledger for Codey's built-in events and event
projections. It is documentation plus tests, not runtime configuration. It does
not create an event bus, dispatch work, alter routing, or change UI/SSE payload
shapes.

`model_visible=true` means the event projection can affect text sent to a web
model. Those rows must be covered by Prompt Envelope and Run Trace. `ui_visible`
uses `false`, `sse:<surface>`, or a known UI surface name. `durable_state` uses
the stable capability durable-state names or `none`.

| event_id | producer | consumers | capability | durable_state | model_visible | ui_visible | policy_required | trace_required | privacy_boundary |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| run_event.turn | codey.events.RunEvent.turn_started | task_runner, run_ledger, sse:chat_stream | agent_runner | run_ledger | false | sse:chat_stream | false | false | UI/SSE and ledger projection only; Review model-visible recent log is declared separately |
| run_event.tool_start | codey.events.RunEvent.tool_started | task_runner, run_ledger, sse:chat_stream | tool_runtime | run_ledger | false | sse:chat_stream | true | false | tool name, display path, activity, and optional command projection only |
| run_event.tool | codey.events.RunEvent.tool_finished | task_runner, run_ledger, sse:chat_stream | tool_runtime | run_ledger | false | sse:chat_stream | true | false | presentation result, status, booleans, exit code, and managed-output metadata only |
| run_event.info | codey.events.RunEvent.info | task_runner, run_ledger, sse:chat_stream | agent_runner | run_ledger | false | sse:chat_stream | false | false | bounded status text and optional provider names |
| run_event.status | codey.events.RunEvent.status | task_runner, run_ledger | agent_runner | run_ledger | false | false | false | false | bounded local progress text only |
| sse.run_event_ui_payload | codey.events.run_event_ui_payload | task_runner, sse:chat_stream | agent_runner | none | false | sse:chat_stream | false | false | existing UI/SSE payload projection only; no extra durable copy |
| run_ledger.model_reply | codey.agent | run_ledger, receipt_projection | run_ledger | run_ledger | false | false | false | false | bounded model reply record for task receipt projection |
| run_ledger.tool_started | codey.task_runner | run_ledger, receipt_projection | run_ledger | run_ledger | false | false | true | false | action_policy-checked tool-start projection without source bodies or long command output |
| run_ledger.tool_finished | codey.task_runner | run_ledger, receipt_projection | run_ledger | run_ledger | false | false | true | false | action_policy-checked presentation and audit metadata; managed output handle only |
| run_ledger.provider_failure | codey.task_runner | run_ledger, receipt_projection | run_ledger | run_ledger | false | false | false | false | provider id, phase, bounded reason code, and recovery metadata |
| run_ledger.changes_collected | codey.task_runner | run_ledger, changes_drawer, receipt_projection | changes_presenter | run_ledger | false | changes_drawer | false | false | file paths, counts, and diff summary metadata only |
| run_details.summary | codey.run_details | server, chat_stream | run_details | none | false | chat_stream | false | false | bounded user-facing run explanation derived from ledger and trace metadata only |
| run_trace.prompt_sections | codey.prompt_envelope | run_trace, prompt_envelope | prompt_envelope | run_trace | false | false | false | true | section names, source refs, lengths, truncation flags, and hashes only |
| run_trace.policy_decisions | codey.action_policy | run_trace, policy_guard | policy_guard | run_trace | false | false | true | true | decision, guard id, reason code, phase, subject ref, and display digest |
| run_trace.fallbacks | codey.task_runner | run_trace, provider_factory | provider_factory | run_trace | false | false | true | true | provider ids, phases, bounded reason codes, and recovery outcome |
| run_trace.provider_failures | codey.task_runner | run_trace, provider_factory | provider_factory | run_trace | false | false | false | true | provider id, phase, bounded error class, and retry/fallback metadata |
| review.recent_log | codey.events.render_run_event | review_runner, prompt_envelope, run_trace | review_runner | none | true | false | true | true | bounded recent run log rendered for Review prompt, including writer reply and tool summaries from the current run |
| tool_outcome.model_text | codey.tool_runtime | agent_runner, prompt_envelope, run_trace | tool_runtime | none | true | false | true | true | bounded model-facing tool text only; presentation, audit, and canonical projections excluded |
| tool_outcome.presentation | codey.tool_runtime | codey.events, task_runner, sse:chat_stream, run_ledger | tool_runtime | none | false | sse:chat_stream | true | false | UI-facing status/result fields with managed-output handle metadata only |
| tool_outcome.audit | codey.tool_runtime | run_ledger, run_trace | tool_runtime | run_trace | false | false | true | true | small JSON-safe audit facts, exit code, truncation facts, and managed-output metadata |
| tool_outcome.canonical | codey.tool_runtime | agent_runner, tests | tool_runtime | none | false | false | true | false | small JSON-safe internal facts only |
| research.notes | codey.research.runner | research_drawer, run_ledger | research_runner | research_notes | false | research_drawer | true | false | note ids, titles, bounded snippets, and provenance refs |
| research.sources | codey.research.runner | research_drawer, run_ledger | research_runner | research_provenance | false | research_drawer | true | false | source ids, URLs after URL policy, titles, and bounded extraction summaries |
| research.synthesis | codey.research.runner | agent_runner, prompt_envelope, run_trace, research_drawer | research_runner | research_notes | true | research_drawer | true | true | bounded synthesis text and source refs; fetched bodies are not persisted here |
| research.object_model | codey.research.object_model | research_runner, run_trace | research_object_model | run_trace | false | false | false | true | record id, answer status, source/evidence/claim counts, assumption counts, and record digest only in trace; bounded record remains internal |
| local_context.refs | codey.context_source | agent_runner, prompt_envelope, run_trace, local_context_drawer | local_context | local_context | true | local_context_drawer | true | true | bounded file refs, symbols, ranges, and hashes rather than source text dumps |
| local_context.actions | codey.task_runner | local_context_drawer, policy_guard, run_trace | local_context | local_context | false | local_context_drawer | true | true | action kind, path refs, decision metadata, and bounded display digest |
| ghost.continuity | codey.ghost.continuity | agent_runner, prompt_envelope, run_trace | local_context | local_context | true | false | true | true | bounded continuity facts, scopes, and refs only |
| ghost.directive | codey.ghost.directive | agent_runner, prompt_envelope, run_trace | local_context | local_context | true | false | true | true | bounded local directive text generated from approved Ghost state |
| ghost.work_queue | codey.ghost.work_queue | agent_runner, prompt_envelope, run_trace | local_context | work_checkpoints | true | false | true | true | bounded item ids, status, proof refs, and task summaries |
| ghost.affinity_events | codey.ghost.affinity | local_context, run_trace | local_context | local_context | false | false | true | true | scope ids, weights, event ids, and bounded evidence refs |
| provider.fallback | codey.task_runner | run_trace, run_ledger, policy_guard | provider_factory | provider_health | false | false | true | true | from/to provider ids, phase, reason code, and recovery decision |
| provider.recovery | codey.provider_revival | run_trace, run_ledger, provider_factory | provider_factory | provider_health | false | false | true | true | provider id, bounded health status, and retry metadata |
| managed_output.artifact | codey.managed_outputs | run_ledger, run_trace, policy_guard | tool_runtime | managed_outputs | false | false | true | true | artifact handle, byte counts, hash, and truncation metadata; stored file remains local |
| changes.diff_presentation | codey.changes | changes_drawer, run_ledger | changes_presenter | change_snapshots | false | changes_drawer | false | false | changed paths, counts, and bounded diff presentation metadata |
| changes.snapshots | codey.changes | changes_drawer, run_ledger, policy_guard | changes_presenter | change_snapshots | false | changes_drawer | true | false | snapshot ids, paths, counts, and restore/action decision metadata |
