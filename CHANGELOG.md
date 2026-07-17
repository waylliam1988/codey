# Changelog

[中文版本](CHANGELOG.zh-CN.md)

This file records Codey's release history. The newest release appears first.

## 0.1.52 - Provider Send Loop Consolidation

- Maintainability: added shared provider send-loop helpers for response-watch
  lifetime, response stability state, completion-flow checks, flow response
  reads, and standard timeout recovery.
- Scope: migrated GLM, Qwen, DeepSeek, and MiMo to the shared helpers while
  keeping provider-specific submission, completion, retry, and response-reading
  behavior inside each web driver.
- Safety: no UI changes, no selector changes, no provider base class, and no
  broad `run_send_flow` callback framework were introduced.

## 0.1.51 - Shell Approval Follow-up

- Follow-up: approved shell results now include short internal hints about
  failed commands, truncated output, PATH refreshes, dev-server ambiguity,
  publish confirmation, and trusted local checks when relevant.
- Safety: follow-up hints do not execute commands, retry installs, or change the
  UI. Writer still has to request any next tool or shell approval explicitly.

## 0.1.50 - Setup-Aware Shell Approval

- UX: shell approval cards now use a neutral `Approval required` label and show
  concise risk notes for dependency installs, system installs, external source
  retrieval, publishing, dev servers, and generic shell commands.
- Context: after the user approves setup-like shell commands, Codey sends the
  Writer a bounded read-only `Setup Context` with local tool availability,
  project manifests, lockfiles, and scoped setup notes. The context is not a new
  model tool and is not injected into normal prompts.
- Safety: setup context never installs, clones, writes files, or networks on its
  own. It reuses sensitive-path filtering, avoids absolute tool paths, reports
  listing limits, and keeps shell execution behind the existing approval flow.

## 0.1.49 - Tool Start Visibility

- UX: Agent tools now emit a lightweight `tool_started` event before local
  execution begins. The web UI shows a quiet pending tool line such as
  `read app.py -> Reading app.py`, then replaces it with the final tool result.
- Design: production tool execution remains serial and observable. The pending
  line reuses the existing monochrome `.tool-line` style; there is no spinner,
  progress system, parallel runner, or ToolSpec registry.
- Safety: `tool_started` events are UI/CLI visibility only and do not count as
  execution evidence, reviewer recent-log facts, or progress toward task
  completion.

## 0.1.48 - Tool Function Injection and Parallel Probe

- Improvement: Agent runtime now supports explicit `AgentToolFns` injection, so
  tests and manual probes can replace tool functions without monkeypatching
  `codey.agent` globals.
- UX decision: Codey keeps production `read`, `ls`, and `search` execution
  serial by default. A deterministic probe showed local wall-clock speedups from
  concurrent read-only batches, but serial tool events preserve step-by-step
  observability, which better matches Codey's quiet local developer-tool feel.
- Safety: bounded file scanning/search paths now check cooperative cancellation
  during long loops.
- Tests/probes: manual A/B probes now use explicit tool-function injection
  instead of monkeypatching `codey.agent` globals. The read-only parallel probe
  remains as a script-local experiment and documents why production Codey does
  not enable read-only concurrency by default.

## 0.1.47 - Search Omission Coverage

- Bugfix: `grep` / `search` now reports non-UTF-8 and unreadable files instead
  of silently treating omitted files as a complete no-match result. The fix is
  intentionally local to the Writer search tool: existing oversized, read-budget,
  and bounded-scan messages are unchanged, and hidden advisor search is not
  migrated.

## 0.1.46 - Coverage-Aware References

`find_references` now reports when its bounded lexical scan skipped files that
could still contain references. The low-level reference scanner records compact
`ScanReport` facts for oversized files, unreadable files, and non-UTF-8 files
without exposing file contents. The Writer-facing tool renders those facts as a
short `Scan coverage` note and marks the tool result as truncated so the JSON
tool protocol also warns models not to treat omitted content as clean.

This is intentionally a narrow production slice. Hidden project-audit advisors
still receive the old low-level reference output, and Project Map, Verification
Map, persistent indexes, cache layers, and ScanPolicy profiles remain unchanged.
The manual scan-coverage A/B probe now reconstructs the old low-level baseline
and compares it with the production Writer coverage renderer.

Live A/B across DeepSeek, MiMo, Qwen, and GLM confirmed the intended behavior.
GLM's old baseline still made a false complete-scan claim and a confident
unused conclusion; the coverage arm made the skipped oversized file explicit
and produced a safe incomplete-scan answer.

## 0.1.45 - Provider Adapter Self-Repair

Codey can now attempt a bounded background repair when a web provider adapter
itself breaks and control-level or Flow-level recovery is not enough. Structural
provider failures can enqueue a deduplicated self-repair job without blocking
new user tasks. Failed repairs stay queued behind a cooldown instead of being
dropped.

Repair runs in a separate Python subprocess. A healthy helper model receives
only the broken provider id, bounded failure context, allowed adapter source
files, and read-only provider tests. It may modify only the target provider's
adapter files inside a temporary sandbox; core files, test files, registry,
tool runtime, server, supervisor, recovery, and safety modules remain outside
the v1 self-modification boundary.

Candidates must pass the repair policy, `py_compile`, Ruff, the corresponding
provider unit test module, and a neutral marker canary before they become a
local provisional override. Overrides load only through a child Provider worker
process, not the main Codey process. The child uses a fresh background tab in
the durable logged-in Codey browser profile, so it does not need cookie copying
and does not steal the user's current chat tab. The parent can close that
temporary tab by CDP target id before killing a stuck worker.

Adapter overrides are versioned under the local state directory, record the
built-in Codey base hash, promote from provisional to active only after natural
successes, and roll back after repeated structural failures. Helper models are
tried in sequence when a candidate is invalid, fails policy, fails checks, or
fails canary.

Live smoke verified the final shared-profile fresh-tab path across DeepSeek,
Qwen, MiMo, and GLM for both the repair helper and candidate worker canary.
Qwen exposed the key design fix: isolated profiles could look logged in but
fail to submit; fresh background tabs in the main Codey browser profile submit
normally.

## 0.1.44 - Focused Project Map

Project Map now adds a bounded `Focused subtree` section for task-aware deep
repository navigation. It scans source files under fixed file, directory,
single-file-size, total-byte, and output-character budgets, then shows only the
highest-scoring module with relative paths, source/test labels, and symbol
signatures. It does not show source bodies, create an index, persist data, add
UI, or call an extra planner model.

The Focused subtree is emitted only when a task is present and only when the
normal Symbol overview is likely insufficient for a larger or budget-limited
repository. When it appears, it replaces the ordinary Symbol overview so the
Project Map stays focused on the deep task-relevant module instead of spending
tokens on low-relevance early files.

Qwen readiness is also stricter and faster: Codey now waits for the chat input,
bootstrap signal, and two consecutive identical non-empty model selector reads
before sending. A cleared input alone no longer counts as a successful
submission; Qwen submit confirmation now requires a stop indicator or response
count increase.

Manual probes recorded that two pre-scope approaches did not earn production
adoption. The retained production path is the lighter layered map: on a deep
synthetic monorepo across DeepSeek, MiMo, Qwen, and GLM, Focused subtree
improved first-file selection from 0/16 to 16/16 while reducing prompt
characters from 53,424 to 33,564 in the post-merge live verification.

Internally, project task context preparation was extracted from `TaskRunner`
into `codey/project_task_context.py`. The builder owns verified facts, Project
Map rendering, checkpoint resume/start, checkpoint prompt construction, and
initial verification candidates. `TaskRunner` still owns Writer execution,
Review, receipts, conversation state, provider failover, and the explicit
evidence seed/invalidation calls.

The diff-review lifecycle is also isolated in `codey/review_coordinator.py`.
The coordinator handles diff retry before review, reviewability checks,
review-unavailable fallback, reviewer follow-up creation, repair dirty-state
tracking, and the narrow green-check inheritance rule after review repair.
Reviewer connection, Writer failover, receipts, ProjectFacts, and conversation
state remain in `TaskRunner`.

## 0.1.43 - Quiet UI Persistence and Sidebar Polish

UI state persistence is now debounced on the hot SSE path. Streaming turn,
tool, and info events coalesce their full localStorage/server saves, while
discrete user actions and terminal task events still flush immediately. This
reduces hidden serialization, network POST, and atomic-write churn during long
tasks without changing the persisted state shape.

Native browser `prompt()` and `confirm()` calls have been removed from the
sidebar. Chat and project rename now use inline inputs, and destructive menu
actions use a quiet two-step confirmation inside the existing monochrome menu.

Consecutive read-only tool rows now fold at render time into compact groups
such as `read · 5 files`. A single read/search/list/reference row remains
visible; only consecutive rows of the same safe kind are grouped. Edit, run,
shell, and error rows stay expanded.

Internally, Writer provider failover was extracted from `TaskRunner` into a
small tested state machine. The refactor does not change provider protocol or
user-facing behavior, but keeps provider takeover, shared turn budget, canary,
checkpoint refresh, and Stop priority easier to prove.

## 0.1.42 - Broader Checks and Quiet Markdown

Controlled `run` now accepts more common verification commands without opening
the unsafe shell path: `ruff check`, `ruff format --check`, `mypy`,
`python -m mypy`, `python -m ruff`, safe `make` targets, `bun test` or
allowed `bun run` scripts, and safe Deno test/lint/check/fmt forms. Mutating or
installing forms such as `ruff --fix`, `ruff format` without `--check`,
`mypy --install-types`, `make deploy`, `bun install`, and `deno run` remain
blocked.

Full verification suites now receive a 300-second timeout while quick commands
keep the existing 90-second budget. Timeout feedback explicitly states that a
timeout is not a test failure and asks the Writer to rerun a smaller subset
instead of guessing a code fix. Literal grep output also suggests narrowing the
query or path when it reaches the match limit.

Assistant replies in the local UI now render a small, monochrome Markdown
subset: code blocks, inline code, bold, headings, and simple lists. Code blocks
get a quiet copy button. There is still no syntax highlighting, no new color
palette, and no additional mode or panel.

## 0.1.41 - Smart Pagination Hint

Paged `read_file` results now include the exact next JSON tool call when more
content remains. Codey keeps the existing complete-line paging behavior and the
older `next offset` text, but adds a concrete `read_file` call with the same
path, next offset, and effective limit so the Writer does not have to infer how
to continue reading a large file.

The hint is generated with JSON escaping, appears only when another page exists,
and does not change read budgets, file contents, tool protocol, or truncation
semantics.

## 0.1.40 - Bounded Stacktrace Pruning

Controlled `run` output now folds obvious dependency stack frames before the
existing middle clipping budget is applied. Python dependency frames from
`site-packages`, `dist-packages`, `.venv`, and `venv` are folded together with
their immediate dependency source line. Node frames are folded only when they
are explicit `at ...` stack entries with a `:line:column` location inside
`node_modules` or `.pnpm`.

Project source frames, assertion messages, exception summaries, test names, and
ordinary logs are preserved. If no dependency stack frame is found, output is
returned byte-for-byte unchanged. The feature changes no tool protocol, exit
code, `ok`, `changed`, or `truncated` semantics.

## 0.1.39 - MiMo Typing Flow and Neutral Web Markers

MiMo completion recovery now reuses its explicit `data-is-typing` transition.
Flow observations distinguish true, false, and unavailable states, so a missing
attribute or DOM read error is never treated as completion. Recovery still
requires a previously observed typing state, an explicit transition to false,
non-empty output, and stable text; built-in completion remains the first path.

Short-answer, long-code, and deep-thinking live probes all observed the required
transition without post-completion growth. A forced-Flow run saved a provisional
rule on the first send and promoted it to active on the next natural send.
Browser-visible verification markers, temporary DOM attributes, page globals,
and clipboard sentinels are now product-neutral. Local-only configuration names
remain unchanged.

## 0.1.38 - Bounded Provider Flow Recovery

Provider recovery bundles can now carry one bounded web-chat state rule in
addition to verified controls. A Flow Recipe is built only from a fixed set of
boolean observations, contains no selectors, JavaScript, URLs, arbitrary
actions, page text, or project data, and shares the existing provisional,
promotion, failure-counting, and rollback lifecycle.

Completion recovery requires stable non-empty output plus a real transition
from generation evidence to terminal evidence; stable text alone is never
enough. Qwen is the first completion pilot through its visible-to-hidden stop
transition. MiMo and GLM deliberately keep their built-in completion behavior
when no equally reliable terminal evidence exists. Four-provider Edge/CDP
control fault injection passed, and a stricter Qwen live run completed with its
built-in completion check disabled, then reused and promoted the recovered
Flow on the next send.

## 0.1.37 - Python Syntax Regression Hint

Successful replacement edits to Python files now receive one bounded syntax
regression hint when the original file parsed successfully but the final edited
content does not. The edit remains written and successful: Codey does not
rollback, run commands, or treat the hint as a passed check. Existing-invalid
files, valid edits, non-Python files, and files above a 128K-character parsing
budget receive no hint.

DeepSeek, Qwen, MiMo, and GLM live A/B fault injection all avoided one failed
test run while preserving correct final code and independent test success.
Three providers also reduced turns or total tool calls; valid-edit controls
produced zero hints across all four providers.

## 0.1.36 - Provider Revival and Writer Takeover

Provider recovery now works as one bounded transaction across the message box,
send button, and answer read. When local discovery is uncertain, Codey can ask
up to three healthy sibling models to select among sanitized structural
candidates, verify the choice through one real send, save a provisional control
bundle atomically, promote it after the next natural success, and roll it back
after explicit repeated control failures.

A passive provider-health circuit now distinguishes structural failures,
transient errors, rate limits, uncertain submissions, and authentication or
challenge states. A Writer that fails with a typed provider-page error can hand
the unfinished task to a healthy sibling in a strict fresh chat using bounded
checkpoint facts. Switches and turn budgets remain bounded; Stop, ordinary
tool failures, protocol failures, and uncertain submissions never trigger an
unsafe resend. Four-provider Edge/CDP fault injection verified recovery and
persisted reuse for DeepSeek, Qwen, MiMo, and GLM.

## 0.1.35 - Default Post-edit Verification

Code changes now receive one bounded verification reminder when Codey can prove
there is a unique, runnable check for the changed files. Candidates come only
from previously successful checks or explicit pytest, npm, Cargo, and Go
project configuration. Existing green checks after the latest edit are reused;
documentation-only changes, ambiguous commands, missing executables, and
cross-ecosystem matches do not enable the gate. Codey never installs or runs a
command automatically, and a failed default check is not repeated forever.
Manifest candidates are refreshed at completion so edits to project scripts
cannot leave the gate using stale commands.

## 0.1.34 - Bounded Edit Failure Context

Failed exact replacements now return bounded current-file evidence when a
unique lexical anchor can be proven. Non-unique replacements report up to three
exact start lines. The write decision remains fully exact: Codey never applies
a closest match, never returns partial long lines as copyable code, and never
uses an in-memory partial batch as disk evidence. Normal successful edits have
no added prompt cost or output.

## 0.1.33 - Read-before-edit Guard

Added a run-scoped guard that rejects replacement edits to existing files until
the Writer has successfully read that file in the current agent run. Full
`content` writes are limited to new-file creation; existing files must use exact
replacements. Files created or changed during the run become known for follow-up
replacement edits. This keeps Symbol overview as navigation help without
letting it become a substitute for inspecting real file contents. DeepSeek and
GLM also auto-click their visible rate-limit retry buttons after a short
cooldown. The initial project prompt now omits absolute temporary paths and the
empty instructions section; repository instructions are included only when an
`AGENTS.md` or `CLAUDE.md` file exists.

## 0.1.32 - Bounded Symbol Overview

Added a task-aware Symbol overview inside the existing Project Map so the
Writer starts with better file and symbol navigation hints before its first
read. It remains bounded and local-only: no new UI, public tool, cache, index,
embedding, LSP, or source body injection. Qwen also gained a narrow recovery
for redirect aborts and one stalled-response retry.

## 0.1.31 - Structured Execution Evidence

Added a bounded in-memory execution ledger so Verification Map, Review, receipts, and successful project facts use the same read, search, edit, truncation, and post-edit check evidence.

## 0.1.30 - Simplified Navigation Tooling

Removed the withdrawn `outline_file` tool after live evaluation showed that Project Map, literal `grep`, `find_references`, and offset `read_file` formed the more reliable navigation path.

## 0.1.29 - Verification Map

Added a hidden, bounded map of test candidates and checks observed after the latest edit for the Reviewer. It is evidence for verification decisions, not impact or coverage proof.

## 0.1.28 - Durable Execution Checkpoint

Added session-scoped recovery facts for unfinished project work: changed-file hashes, fresh successful checks, the last edit or run action, and the stop reason.

## 0.1.27 - Find References and Bounded Scans

Added bounded lexical reference hints and a shared streaming scanner for references, grep, and hidden audits, with explicit incomplete-result reporting.

## 0.1.26 - Outline File Experiment

Introduced `outline_file` as a bounded navigation experiment. Natural-use evaluation later showed weak adoption, and the tool was fully removed in 0.1.30.

## 0.1.25 - Hidden Project Map

Added a bounded, read-only project structure map for Writer, hidden advisors, and Reviewer without indexing source, adding RAG, or exposing a new UI.

## 0.1.24 - Hidden Change Briefs

Added a private, bounded ChangeBrief shared by Writer and Reviewer, plus verified successful-change facts derived from real edits and checks.

## 0.1.23 - Browser Launch Robustness

Added Edge-first browser discovery with Chrome fallback, clearer WebView startup failure handling, and explicit truncation markers for tool and review results.

## 0.1.22 - Durable Conversation Handoff

Added a bounded visible-conversation excerpt to factual handoff when a browser-model context is no longer trusted.

## 0.1.21 - Durable Chat State

Persisted bounded sidebar and chat state, added quiet copy controls, and reconciled Send/Stop state across restarts.

## 0.1.20 - Quiet Chat Controls

Refined the compact chat controls and interaction states without adding a new workflow or mode.

## 0.1.19 - MiMo Answer Completion

Separated MiMo send-button detection from answer-completion detection and used the answer DOM to avoid premature completion.

## 0.1.18 - Provider Reliability

Tightened MiMo, Qwen, and GLM browser-state handling, local JSON protocol validation, and review-repair check freshness.

## 0.1.17 - Hidden MoA Layer

Added hidden owner-first multi-model advice for normal chat and new projects, plus bounded read-only advisor audits for existing projects.

## 0.1.16 - Plain Chat and Project Discussion

Kept New Chat project-free while allowing one project conversation to move naturally from discussion to reading and editing.

## 0.1.15 - GLM Provider

Added GLM as the fourth supported web model and consolidated provider registration and smoke selection.

## 0.1.14 - Protocol Efficiency and Safety

Unified the local tool contract, bounded safe parallel reads, paged large files, and made multi-replacement edits atomic.

## 0.1.13 - Runtime Ownership Cleanup

Unified Git and snapshot change handling, centralized runtime storage, and made provider-session ownership explicit.

## 0.1.12 - Resilient Run Reconciliation

Added bounded backend run snapshots and ordered UI reconciliation across refreshes and short connection interruptions.

## 0.1.11 - Responsive Stop

Made Stop interrupt provider waits, recovery, review, and controlled commands, and preserved both ends of long command output.

## 0.1.10 - ProfileDoctor Recovery

Added a bounded, sanitized second recovery step that can ask an already-open model to choose among structural browser-control candidates.

## 0.1.9 - Bounded Provider Recovery

Added versioned provider profiles and conservative, verified rediscovery for changed message boxes, send buttons, and answers.

## 0.1.8 - Durable Local Continuity

Persisted a small set of proven project commands, bounded factual chat snapshots, and non-Git recovery baselines.

## 0.1.7 - Structured Runtime

Introduced structured tool outcomes and events, separated task orchestration from HTTP transport, and removed UI parsing of prose logs.

## 0.1.6 - Hidden Context Handoff

Added bounded factual summarization and fresh-chat continuation near the shared context budget.

## 0.1.5 - Control Teaching Cleanup

Refined recovery and cleanup around user-taught browser controls while keeping teaching as a quiet last resort.

## 0.1.4 - Task Receipts

Added compact task receipts showing changed files, check status, and restore availability.

## 0.1.3 - Durable CDP Browser Reuse

Reused an existing Edge CDP browser and model tabs across Codey UI restarts before launching a new browser.

## 0.1.2 - Provider Status and Composer Shortcuts

Improved provider status feedback and keyboard-oriented message composer controls.

## 0.1.1 - Stability Smoke

Added release-level stability smoke coverage for the initial local browser-model workflow.

## 0.1.0 - Initial Bilingual Release

Published the first bilingual Codey release: a local-first bridge from supported web AI chats to controlled file editing, checks, diffs, and restore.
