# Changelog

[中文版本](CHANGELOG.zh-CN.md)

This file records Codey's release history. The newest release appears first.

## 0.2.11 - Provider Readiness Self-Repair

- Provider diagnostics: added a narrow `readiness_stale` failure kind so
  adapters can report cases where safe DOM facts suggest the page is usable but
  a readiness signal has gone stale.
- Safe failure facts: `ProviderFailure` can now carry a small sanitized facts
  packet for self-repair. Facts use an explicit readiness allowlist
  (`composer_visible`, `send_visible`, `model_selector_text_present`,
  `response_count`, `question_count`, `waited_for`) and ordinary failures still
  omit empty facts from their payloads.
- Self-repair routing: `readiness_stale` is treated as a structural provider
  failure for circuit/self-repair purposes, while keeping the existing rule
  that repairs are queued only after the provider circuit opens.
- Adapter repair prompts: self-repair worker now forwards failure kind, stage,
  and sanitized facts into the adapter repair prompt so the repair model can
  distinguish stale readiness checks from missing controls.
- Boundary discipline: no UI changes, no user-project access, no webpage body
  capture, no new repair framework, and no change to the fresh-chat marker
  canary before enabling a provisional adapter override.

## 0.2.10 - Research Quality and Provider JSON Hygiene

- Web provider JSON hygiene: moved shared JSON-tool reply detection/repair into
  `codey/json_tool_reply.py` and reused it from DeepSeek and Qwen. Qwen no
  longer depends on the stale `/api/v2/models/` bootstrap signal, can finish
  stable JSON tool replies promptly, and prefers DOM JSON when copied text is
  stale or incomplete.
- Research quality gate: final reports now accept common source formats such as
  `[1] https://final-url` and numbered `Available at:` lines while still
  requiring cited source URLs to be opened and evidence-backed. The provenance
  check is stricter for conclusion/evidence/source sections, but lets
  counterpoint and coverage sections mention unopened search-result domains as
  limitations.
- No-source research reports: when a Research run searched but found no citable
  opened source, Codey can now accept a clearly labelled no-citable-source
  report instead of forcing invented citations.
- Research repair UX: quality-gate followups now explain the exact hard
  requirements instead of sending a generic continue prompt, and the Research
  system prompt uses the neutral "local research agent" wording.
- Plain chat display: chat replies now carry `run_id`, `task_done` includes the
  chat answer as its summary, and the frontend de-duplicates `reply` /
  `task_done` answer events so normal chat answers can be restored and shown
  reliably.
- Manual Research A/B: added `tests/manual/deep_research_core_ab.py` as a live
  provider harness for source-search / plan / coverage experiments. The probe
  does not change default production Research behavior.

## 0.2.9 - Runtime Responsiveness Hygiene

- SSE reliability: when a subscriber queue is full, Codey now drops the oldest
  queued event and keeps the newest event instead of silently losing the newest
  state update.
- Research restore responsiveness: restoring Research note changes now schedules
  a coalesced background knowledge index rebuild, so `/api/research/restore`
  can return without waiting for a full vault scan.
- Review responsiveness: rejected Review repair reuses the project map already
  refreshed for the same review cycle instead of refreshing it twice.
- Boundary discipline: no API payload changes, frontend changes, incremental
  indexer, workspace watcher, or background task framework.

## 0.2.8 - Research TaskRunner Hygiene

- TaskRunner hygiene: split `TaskRunner.run()` mode execution into private
  chat, research, hybrid, and project helpers while keeping run reservation,
  provider lifecycle, cancellation, and terminal `finish_run()` handling in the
  main orchestration method.
- Boundary discipline: added only small private frame/work/hook data carriers
  inside `task_runner.py`; there is no new router, strategy system, task mode,
  or module split.
- Compatibility: kept task events, payloads, provider failover behavior,
  review flow, project memory writes, and Research handoff behavior unchanged.

## 0.2.7 - Research Server Hygiene

- Research API hygiene: moved the `/api/research/graph`,
  `/api/research/note`, and `/api/research/restore` response assembly into
  small `server.py` helpers that return `(status, payload)`.
- Run submit hygiene: moved `/api/run` validation and submit response assembly
  into a matching helper, keeping `_submit_task()` and task execution behavior
  unchanged.
- Boundary discipline: kept helpers inside `server.py`; there is no router,
  `server/research.py` module, schema change, or payload/status-code change.

## 0.2.6 - Frontend Research Graph Split

- Frontend hygiene: moved the Research drawer Graph implementation out of the
  monolithic `index.html` script and into `codey/web/assets/research_graph.js`.
  The main HTML now keeps only the drawer wrapper and callbacks for depth,
  note handoff, and source opening.
- Static asset boundary: added a narrow whitelist route for
  `/assets/research_graph.js`; Codey still does not expose `codey/web` as a
  general static directory.
- Compatibility: kept the existing Graph UI, canvas layout, CSS, callbacks,
  and global browser shape. There is no ES module migration, bundler, CSS
  split, or frontend framework change.
- Cache hygiene: the HTML loads the graph script as
  `/assets/research_graph.js?v=0.2.6` so webview/browser sessions pick up the
  split module during the release.

## 0.2.5 - Research Graph

- Research drawer Graph: added an Obsidian-like local graph tab that visualizes
  the current Research run as note/source nodes connected by
  `derives` / `supports` / `contradicts` / `implements` / `verifies` links.
  The graph uses a lightweight canvas force layout with hover highlighting,
  click details, source URL opening, Depth 1/2, and Reset.
- Graph read model: added `codey/knowledge/graph.py` plus bounded index queries
  for notes, incoming/outgoing links, and note sources. Markdown notes remain
  the source of truth; SQLite remains a rebuildable cache; the graph is loaded
  on demand through `/api/research/graph`.
- Provenance display: URL sources become virtual `source_url` nodes with
  virtual `cites` edges for display only. `cites` is intentionally not a
  persistent note link kind.
- Counterpoint hygiene: virtual counterpoint nodes are created only from the
  current run's counterpoint payload when there is no real `contradicts` link.
  The graph does not parse synthesis Markdown sections.
- UI language: the drawer graph keeps Codey's monochrome design, using grey
  nodes and lines by default. `--ok-dot` appears only as an interaction accent
  on the hovered node and its connected edges.

## 0.2.4 - Research PDF Intake

- PDF source intake: `open_url` can now read text PDFs directly. There is no
  `open_pdf` tool, PDF mode, or extra UI button; PDF is handled as another
  Research source type.
- Bounded extraction: Codey reads a bounded page range by default, streams PDF
  downloads with a hard byte cap, caps extracted text, and returns neutral
  `SKIPPED` results for scanned, oversized, empty, or extraction-failing PDFs.
  PDF redirects are followed manually and checked against URL policy before
  each next request, so a public PDF URL cannot silently redirect into local or
  private network targets.
- Page-aware evidence: the Evidence Ledger records `content_kind`, MIME type,
  page count, pages read, truncation state, and page locators for snippets.
  `knowledge_write` can accept `evidence.page`, infer pages from snippets, and
  replace bad excerpts with exact text from the opened PDF page.
- Report quality gate: final reports can cite PDF evidence as `[1 p.4]` or
  `[1 pp.4-5]`. Page citations pass only when the cited PDF page was read and
  has snippet-backed evidence.
- UI and handoff: the Research drawer shows PDF page locators in `Evidence`
  and PDF/page/truncation metadata in `Sources`. Synthesis notes and Project
  Briefs carry the same page-aware evidence without injecting the full vault.
- Dependency: added `pypdf>=6.0,<7` for pure-Python PDF text extraction.

## 0.2.3 - Research Provenance Polish

- Research provenance: explicit URL citations remain exact, but source-quality
  text can now name a parent site domain for an opened child host. For example,
  opening `docs.python.org` allows a quality note to say `python.org`, while
  opening `python.org` still does not allow a report to claim
  `docs.python.org`.
- Research quality gate: URL spans are excluded from bare-domain scanning, so
  paths such as `pathlib.html` are not misread as unopened source domains.
- Project memory: added an integration regression for the verified
  implementation memory path, covering implementation notes, verification
  notes, and `implements` / `verifies` links back to the research synthesis.
- Hygiene: removed a stale `EvidencePack` import from the task runner.

## 0.2.2 - Research Report Quality

- Evidence Ledger: each Research run now records search queries, ranked search
  results, opened requested/final URLs, retrieval timestamps, source-quality
  hints, and short evidence snippets.
- Report quality gate: final Research reports must include `Conclusion`, `Key
  evidence`, `Counter-evidence / limitations`, `Source quality`, `Search
  coverage`, and `Sources`. Numbered citations must map to sources Codey
  actually opened as final URLs in the run, and each cited source must have at
  least one saved evidence snippet copied from the opened page text.
- Evidence discipline: evidence snippets attached to notes must appear in the
  opened page text. Search results are still not evidence until `open_url`
  reads them, and low-quality reports are sent back to the researcher for
  revision instead of being saved as synthesis.
- Research UX recovery: unreadable sources such as PDFs are now shown as
  neutral `SKIPPED` tool results so the researcher can continue with readable
  HTML sources. If a model supplies a paraphrased evidence excerpt for an
  opened page, Codey replaces it with an exact opened-page snippet and keeps
  the quality gate strict.
- Report parsing: the quality gate accepts common numbered headings such as
  `1. Conclusion` / `一、结论` and source rows written as Markdown links, while
  keeping citation provenance strict.
- Advisors: Research MoA advisors now receive a richer read-only EvidencePack
  with citations, evidence items, coverage, notes, and source URLs; advisors
  still cannot browse or write the vault.
- UI and handoff: the Research drawer now has `Evidence`, `Sources`, and
  `Notes` tabs, with search coverage shown inside `Evidence` as supporting
  audit detail. Project handoff carries a bounded Research Brief with citation
  map, evidence items, counterpoints, and source-quality risks instead of the
  full vault.

## 0.2.1 - Research Polish and UI Follow-through

- Local provider: removed the explicit saved-key clearing checkbox. Leaving the
  API key field blank keeps the saved key; entering a new key replaces it on
  `Connect`.
- Research evidence flow: citing a search-result URL before opening it now
  returns `NEEDS_OPEN` instead of a red error. `NEEDS_OPEN` is a neutral
  `needs_action` tool status, not a saved note and not a changed tool result.
  Both generic run events and the Web/SSE production event path carry the same
  status.
- UI: active `Research` now only brightens the text; it does not add a border,
  background, or font-weight change. Assistant replies render expanded by
  default, with `Collapse` available for long answers.
- Markdown: assistant report rendering now supports `#` through `######`
  headings and basic nested lists while staying dependency-free and
  monochrome.

## 0.2.0 - Research, Knowledge, and Local Models

This is a major workflow release. Codey is no longer only a local coding loop:
it can explicitly research a question, save grounded local notes, carry a
bounded synthesis into a project, and remember verified implementation facts.

- Research: the composer context now includes `Research`. When enabled for a
  message, Codey can search the web, open pages, enforce URL policy, write
  source/fact/synthesis notes, and reject final citations that were not opened
  in the run.
- Knowledge: research notes are stored in a local Markdown vault with a
  rebuildable SQLite FTS index, per-run restore, note links, and a bounded
  Research Brief for project handoff. Project source code is not copied into
  the vault.
- Project handoff: after research, choosing a folder injects only the bounded
  Research Brief into the Writer prompt. Follow-up Research and Project Hybrid
  runs receive bounded prior chat context so prompts like "continue researching
  that plan" keep working.
- Local provider: `Local` connects to OpenAI-compatible endpoints such as LM
  Studio, Ollama, or llama.cpp. The compact config popover supports base URL,
  model id, optional API key preservation, and replacing the saved key by
  entering a new one.
- Safety and reliability: web provider sends stay on the browser-worker thread;
  only thread-safe Local sends use cancellable background send. Browser-worker
  calls are reentrant, preventing Research search self-deadlocks. Hidden-browser
  runtime code was removed from Codey's runtime path.
- UI: Research is a lightweight composer token beside `Choose folder` and the
  model name, not a separate app or another button beside the model selector.
  The Research drawer shows notes, source URLs, synthesis, and restore state.

## 0.1.63 - Single Provider Self-Review

- Review: when no different provider is available for final diff review, Codey
  now opens a temporary fresh tab for the Writer's same provider and runs a
  clearly labelled self-review pass.
- Repair loop: self-review findings reuse the existing Reviewer-to-Writer
  repair path, but Writer follow-up wording no longer claims that a second
  model reviewed the diff.
- Safety: true second-model review is still preferred. Self-review does not
  clear the Writer provider session, closes the temporary reviewer tab in a
  `finally` block, and failures continue to fall back to the existing
  single-model result.

## 0.1.62 - Review Impact Map

- Review: final diff review now receives a short, bounded Review Impact Map
  after the ChangeSet summary. It lists obvious changed symbols plus local
  caller/test reference hints so reviewers can inspect likely blast radius.
- Reliability: changed-symbol extraction is centralized in `changed_symbols.py`
  and is reused by Verification Map. Rename cases use the old symbol name for
  reference scans while preserving the new changed-file path from ChangeSet.
- Safety: the map is review-only, source-body-free, best-effort, and explicitly
  labelled as not coverage proof. Writer behavior, UI, tools, provider logic,
  and `/api/changes` remain unchanged.

## 0.1.61 - ChangeSet Anchored Review

- Review: final diff review now receives a structured ChangeSet summary before
  the raw diff, including changed files and parsed hunk ranges.
- Reliability: reviewer findings may include optional `hunk_index`,
  `new_line`, or `old_line` anchors. Codey validates those anchors against the
  actual changed hunks before passing them back to the Writer; path-only
  findings remain valid.
- Compatibility: `/api/changes`, the UI diff drawer, receipts, restore, and the
  underlying `changes.py` dict output are unchanged. Git rename labels such as
  `old.py -> new.py` are normalized only inside the ChangeSet interpretation
  layer so review hunks attach to the new path.

## 0.1.60 - CLI Agent JSONL

- CLI: `python -m codey agent --json ...` now emits one JSON object per stdout
  line, including a session header, agent start/end records, turn events,
  status/info events, and bounded tool start/result records.
- Integration: JSONL output is intended for scripts, CI wrappers, benchmarks,
  and external launchers that need stable progress and final-result fields
  without parsing human-readable stderr logs.
- Safety: normal CLI mode is unchanged. JSONL tool records include only compact
  result summaries, bounded text fields, and command/status metadata; provider,
  server, UI, agent, and tool execution behavior are unchanged.

## 0.1.59 - Package Manager Setup Hints

- Reliability: setup context now uses the same Node package-manager detection
  as trusted verification discovery: `packageManager` first, then local
  lockfiles, then parent lockfiles, then `npm`.
- UX: setup hints now recommend concrete install commands such as `pnpm install`,
  `yarn install`, or `npm ci or npm install` instead of generic package-install
  wording.
- Consistency: shell approval follow-up now renders trusted check candidates
  through the shared verification candidate formatter, keeping cwd-scoped
  command lines aligned with Project Map and Verification Map.

## 0.1.58 - Scoped Successful Change Checks

- Reliability: successful-change facts now preserve the working directory for
  local checks, so scoped validations render as `backend/: npm test` instead of
  losing the path context.
- Compatibility: existing project fact files with legacy string-only
  `successful_changes[].checks` continue to load with `cwd="."`; new writes use
  structured `{command, cwd}` check records.
- Safety: non-check commands, sensitive commands, and unsafe working-directory
  values remain filtered before they can become durable project facts.

## 0.1.57 - Policy-Sourced Verification Candidates

- Reliability: Project Map and Review Verification Map now receive candidate
  check commands from the same trusted `verification_policy` discovery path,
  instead of letting Project Map infer commands from manifests on its own.
- Review: Verification Map now labels only the uniquely selected, change-relevant
  command as `Recommended local check candidates`; broader commands remain
  available under the weaker broader-candidate label when no unique choice
  exists.
- Cleanup: direct `render_project_map()` calls no longer guess candidate
  commands. Manual probes that need production context now use
  `ProjectTaskContextBuilder`, keeping evaluation scripts aligned with the
  real Writer path.

## 0.1.56 - Composer Folder Label Cleanup

- UX: no-project chats now keep the composer context label as `Choose folder`
  even when the composer contains a draft. The draft-send behavior remains
  explicit through the same folder click, with the longer wording kept out of
  the visible composer chrome.
- Safety: no behavior change to project access. The user still has to choose a
  folder explicitly, and pressing Enter in a no-project chat remains a normal
  chat send.

## 0.1.55 - Draft-to-Project Send

- UX: a plain New Chat can now be attached to a project folder in place from
  the composer context. If the composer has a draft, the same explicit folder
  click keeps that draft and sends it in the same session after the folder is
  chosen.
- Continuity: chat-to-project transitions now preserve the prior conversation
  handoff and visible recent chat facts for the Writer, instead of starting the
  project task without the discussion that led to it.
- Safety: there is no natural-language intent detector and no automatic project
  access. The user must explicitly choose the folder context; pressing Enter in
  a no-project chat remains a normal chat send.

## 0.1.54 - Trusted Verification Discovery

- Reliability: trusted post-edit verification discovery now recognizes more
  safe project checks that were already permitted by the local `run` tool,
  including package-manager scripts selected from `packageManager`/lockfiles,
  `pytest` config, `tests/` unittest discovery, `ruff`/`mypy` config, and simple
  safe Makefile targets.
- Selection: completion-time verification candidates now use a small command
  priority so discovering `test`, `typecheck`, `lint`, `build`, and Makefile
  targets does not make common projects ambiguous. More specific ecosystem
  checks beat Makefile fallbacks.
- Safety: no UI changes, no automatic installs, no shell permission expansion,
  and no automatic execution behavior were added.

## 0.1.53 - CDP Browser Warmup

- UX: launching the UI now schedules a best-effort browser warmup that prepares
  the Codey-controlled CDP browser and opens DeepSeek, Qwen, MiMo, and GLM tabs
  when no provider tab is already visible.
- Safety: warmup does not check login state, send test messages, change UI, or
  bypass provider supervisor health filtering. Existing provider tabs are reused
  and no duplicate provider pages are opened.
- Reliability: warmup runs on the shared browser worker with short timeouts,
  avoids reusing unrelated external CDP browsers, keeps slow provider pages when
  they reached the target URL, and closes failed blank warmup tabs.

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
