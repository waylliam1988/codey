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
- `Research` starts a research run for the current request.
- Automatic mode can route a task to chat, read-only planning, Research,
  Writer, Hybrid, or Review before execution starts.
- Manual mode, project scope, and permission settings still win over automatic
  routing.

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

## Research Loop

Research can search the web, open HTML/PDF sources, save bounded notes, and
produce a cited synthesis. Final claims must bind to saved evidence from opened
sources; search results, local memory, and Ghost continuity are not evidence.

Biomedical and paper-oriented questions prefer PubMed/arXiv article results
when available. Broad landing pages are skipped when a more specific source can
be opened. If a concrete proof gap remains, Codey can run one bounded
evidence-only follow-up and merge fresh evidence deterministically.

## Verification and Completion

When code changes, `done` is not accepted just because the model says it is
done. Codey records local completion proof from fresh checks, changed files,
observed failures, and repair context.

- Fresh passing checks can complete the task.
- Missing, failed, or environment-broken checks block honestly.
- One bounded facts-only repair round may be admitted for observed product
  failures.
- Suspicious edit/test integrity results appear in the task receipt instead of
  being reported as clean.

## Local Memory

Ghost is Codey's bounded local continuity layer. It can remember explicit
preferences, recent verified work facts, project habits, research open
questions, and follow-up work items.

Ghost state remains controllable:

```text
preview
export
delete
reset
disable
```

It is not evidence, not permission, not automation, and not a second agent.

## Runtime and Recovery

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

These surfaces avoid raw prompts, raw model replies, full source files, webpage
bodies, cookies, and secrets.

## Current Boundaries

Codey does not currently expose a public plugin system, does not let Ghost or
World Model decide facts, does not let provider-native search count as Codey
evidence, and does not let adapter repair modify the runtime core.

For planned runtime work, see
[Codey Pi v2-inspired refactor direction](codey_pi_v2_refactor_direction.zh-CN.md).
