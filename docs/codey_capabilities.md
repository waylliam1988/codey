# Codey Capabilities

This page keeps practical feature detail out of the README. It is a product
overview, not a release gate.

## Model Access

- Web providers: DeepSeek, MiMo, StepFun, Qwen, and GLM.
- Local provider: any OpenAI-compatible endpoint with an optional API key.
- No API key is required for web providers; you log in through a dedicated Edge
  or Chrome browser profile.
- Provider-specific browser code is isolated in adapters, so website breakage
  can be repaired without changing the agent core.

## Work Modes

- `New Chat` keeps the conversation detached from local project files.
- `Choose folder` attaches the current conversation to one local project.
- `Research` enables strict evidence, ledger and report requirements for the request.
- Automatic mode lets the first normal model call answer directly or request
  an action within the original task policy. Tools and incomplete answers can
  continue through the same kernel; model mode labels cannot grant permissions.
  There is no separate routing model call, and project writes require the writer lock.
- Manual mode, project scope, and permission settings still win over automatic
  mode.

Coding, planning, Research and authorized mixed tasks share one tool loop.
Web access requires task authorization; ordinary web-assisted coding does not
automatically require a Research notebook. See the
[source ownership map](project_structure.md).

## Coding Loop

Codey can let a model read files, edit files, run allowed commands, inspect
diffs, review changes, and restore snapshots inside the selected project
folder.

The local loop is intentionally visible:

```text
read -> edit -> run/check -> diff -> review -> done/blocked
```

Git improves the workflow when available, but Codey keeps non-Git diff and
restore paths so beginners can start without learning Git first.

Desktop and CLI/headless project tasks use one service composition and the
same review policy: prefer an available web Reviewer, otherwise use a fresh
self-review if allowed. Concrete findings enter at most one Writer repair;
review approval does not replace fresh verification. CLI exposes all task
intents, explicit network/write grants, session continuation and review reuse.
Embedded gates may explicitly pin a Reviewer connector. Presentation and
noninteractive shell approval remain entry-specific.

Text tool calls support bounded read-only batches: `read_files` and `parallel`
validate the whole batch before running its actions in order. Repeated unchanged
tool cycles stop within a bound; new information and successful edits keep going.
Cancellation stops further actions, including later tools in the same batch.

## Research Loop

Research can search the web, open HTML/PDF sources, save bounded notes, and
produce a cited synthesis. Final claims must bind to saved evidence from opened
sources; search results, local memory, and Ghost continuity are not evidence.

The returned and saved synthesis uses the same compiled evidence/citations,
demotes unsupported conclusions, and retains up to four follow-up questions.

Biomedical and paper-oriented questions prefer PubMed/arXiv article results
when available. Broad landing pages are skipped when a more specific source can
be opened. If a concrete proof gap remains, Codey can run one bounded
evidence-only follow-up and merge fresh evidence deterministically.

## Verification and Completion

When code changes, `done` is not accepted just because the model says it is
done. Codey records local completion proof from fresh checks, changed files,
observed failures, and repair context.

- Fresh passing checks can complete the task.
- Explicit requests to skip verification are honored without claiming checks passed.
- Missing, failed, or environment-broken checks block honestly.
- One bounded facts-only repair round may be admitted for observed product
  failures.
- Suspicious edit/test integrity results appear in the task receipt instead of
  being reported as clean.

## Local Memory

Ghost is Codey's bounded local continuity layer. It records completed-round
experience (user words, final answer) and retrieves a
small number of related past rounds on the next normal model call within a
strict char budget (chars, not tokens). Only successfully finished rounds are retrievable, keyed
idempotently by run id. Ghost never adds a model call of its own: no per-turn
routing call, no per-turn extraction call, and no hidden output contract for
the main model to fill in.

Ghost state remains controllable (preview, export, delete, reset, and disable
apply to experience observations as well as older stores):

```text
preview
export
delete
reset
disable
```

It is not evidence, not permission, not automation, and not a second agent.

## Runtime and Recovery

Local HTTP/SSE access requires the browser operator session established by the
Codey launch link. This is separate from task grants; it never grants the model
network, project-write or shell permission. Desktop and CLI/headless derive task authorization through the same rules.

Codey records bounded runtime facts so interrupted work can be explained and
resumed more honestly. Provider sends, tool calls, repair rounds, delivery
receipts, and completion proof are tracked through durable intent/settlement
style records.

The recovery policy is conservative:

- safe read/search effects may be replayed;
- unsafe or uncertain local effects are not repeated silently;
- missing settlement is surfaced as an interrupted or unknown-outcome step;
- Run Details can explain what happened without exposing raw prompts or raw
  outputs.

## Audit Surfaces

Codey keeps quiet audit surfaces for people who need to inspect behavior:

- task receipts;
- Run Details;
- Local context drawer;
- prompt envelope manifests by digest and source refs;
- research evidence/source/note views;
- bounded run traces and ledgers.

Audit summaries and prompt manifests are bounded projections. Local managed
outputs and recovery receipts may retain read source text, webpage bodies and
tool results; conversation and enabled Ghost experience stores also retain
user messages/answers. Prompt traces do not record raw prompts. These stores
have different purposes; the summary view is not a claim that no raw content
exists in local state.

## Current Boundaries

Codey does not currently expose a public plugin system, does not let Ghost or
World Model decide facts, does not let provider-native search count as Codey
evidence, and does not let adapter repair modify the runtime core.

For planned runtime work, see
[Codey Pi v2-inspired refactor direction](codey_pi_v2_refactor_direction.zh-CN.md).
