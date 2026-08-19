# Codey

**Turn web AI models into a local-first coding, research, and controllable memory workspace.**

[![Version](https://img.shields.io/badge/version-0.4.3-blue)](CHANGELOG.md)
[![License: GPL v2](https://img.shields.io/badge/license-GPL--2.0--only-blue)](LICENSE)
[![Local first](https://img.shields.io/badge/local--first-AI%20workspace-2ea44f)](#safety-model)

[中文说明](README.zh-CN.md)

Codey connects to AI chat websites you already use, such as DeepSeek, MiMo,
StepFun, Qwen, and GLM, or to a local OpenAI-compatible model, then gives them
controlled local work loops: chat, research with evidence, read files, edit
files, run tests, show diffs, review changes, restore safely, and carry bounded
local memory/continuity/affinity state that you can inspect, export, delete, or disable.

It is a local-first, low-cost AI coding, research, and controllable-memory
workspace for people who want useful help without wiring paid model APIs into
every project.

No API key is required for web providers. Log in to the web AI in Edge or Chrome, pick a local project folder, and start building. If you run LM Studio, Ollama, llama.cpp, or another OpenAI-compatible local endpoint, choose **Local** and enter its base URL/model once.

Version: `0.4.3`

[Version history](CHANGELOG.md)

---

## At a Glance

- **Use web AI accounts you already have**: DeepSeek, MiMo, StepFun, Qwen, and GLM are supported.
- **Auto picks the right path**: in automatic mode, Codey can choose chat,
  read-only planning, Research, Writer, Hybrid, or Review before the task
  starts, while manual choices and permissions still win.
- **Keep memory controllable**: explicit preferences and short continuity
  context stay in bounded local files that can be previewed, exported, deleted,
  reset, disabled, and quietly maintained after tasks.
- **Audit local context on demand**: open `Local context` from the topbar
  `...` menu to inspect, export, delete, reset, or disable bounded local state
  without adding a persistent sidebar or interrupting the task flow.
- **Trace model input composition quietly**: each run can keep a bounded local
  prompt envelope manifest, so model-visible sections are auditable by digest
  and source refs without saving raw prompts.
- **Keep internal capability boundaries explicit**: Codey keeps a read-only map
  of built-in capabilities and their policy/model-visible/state boundaries
  without exposing a plugin system or changing task behavior.
- **Separate tool results by audience**: local tools return bounded
  model-visible text, UI presentation facts, audit metadata, and small
  canonical facts through one clean contract instead of one shared output
  string.
- **Guard local actions consistently**: local file, run, shell, Research URL,
  provider fallback, and managed-output artifact decisions now pass through one
  monotonic action policy pipeline and are auditable without exposing raw
  commands or URLs.
- **Keep event boundaries testable**: Codey now keeps an Event / Capability
  Matrix for run events, ledger records, trace entries, tool projections,
  Research, Local context, Ghost, provider fallback, managed outputs, and
  changes without adding an event bus or changing UI/SSE payloads.
- **Keep background defaults explicit**: Codey now keeps a tested built-in
  default-strategy catalog for Research-heavy, review-strict, local-only, and
  beginner-friendly tendencies without adding a new UI or changing task
  behavior.
- **Explain a run only when asked**: finished task rows can show a quiet
  `Details` link that opens a short, read-only explanation of the work, model,
  context, actions, safety checks, fallbacks, and verification. It does not
  expose raw prompts, raw output, or internal debug terms.
- **Keep Research conclusions traceable**: after a Research run, Codey now
  builds a deterministic Research object record from opened sources, evidence
  snippets, claims, assumptions, and claim/evidence relations. Search results
  and local memory are not treated as evidence.
- **Know when queued Research is actually done**: queued research follow-ups now
  complete only after a deterministic proof review verifies answer coverage,
  citations, opened-source evidence, locators, and support relations. The review
  also produces quiet planner signals for future follow-up search.
- **Plan the next reliable source type quietly**: Research proof gaps now feed a
  deterministic dry-run planner that can prefer PubMed, arXiv, or local
  project-scoped sources without executing searches or changing model-visible
  tool results.
- **Continue saved work naturally**: when Codey has a queued local follow-up,
  saying "continue" can claim one item and run the right path with proof.
- **Research before building**: click `Research` to let Codey search the web, open HTML/PDF sources, save readable note cards with source chips, visualize the local note/source graph, and produce a cited synthesis with counter-evidence, source quality, and search coverage.
- **Keep code local**: models access only the project folder you choose.
- **Stay oriented while coding**: after each local tool result, Codey reminds
  the model which files were read, which files changed, and which verification
  command best matches the current edits.
- **Carry research into projects**: after research, choose a folder and Codey injects a bounded Research Brief with citations and limitations instead of the whole vault.
- **Controlled tool loop**: read, edit, test, diff, review, and restore.
- **Optional local model**: `Local` connects to an OpenAI-compatible endpoint with optional API key support.
- **Review after edits**: one model can write while another reviews the final
  diff; if no second model is available, the writer can run a labelled
  self-review pass.
- **Beginner-friendly by design**: Git helps, but it is not required.

---

## Why

AI-assisted programming should not require expensive API credits or complicated setup.

Codey is an experiment in making AI coding more accessible:

- use browser-based AI models
- keep code on your own machine
- see exactly what changed
- restore changes when needed
- let beginners start before they understand Git

The goal is not magic. The goal is a small, usable bridge from idea to working code.

---

## Philosophy

Access matters.

If AI programming only works well for people who can pay for expensive API usage, many beginners are locked out. Codey tries a simpler path: use the web AI access people already have, connect it carefully to local files, and make the edit/test/diff/restore loop understandable.

This is not about replacing professional tools. It is about making the first step into programming and creation cheaper, simpler, and more local.

---

## What It Can Do

- Use New Chat for normal conversation without granting access to a project
- Use `Research` from the composer context to search/read HTML and text PDFs, save notes, and produce a grounded synthesis with numbered citations, evidence snippets, counter-evidence, source quality, and search coverage
- Build a local Research object record after each Research run, linking final
  claims only to matching opened-source evidence with the right stance. Claim
  `status` only records `evidence_backed`, `unsupported`, or `assumption`;
  support, refutation, and limits are expressed by relation kind for later
  proof-quality checks
- Turn a plain chat into a project task from the same conversation by choosing a folder from the composer context
- Turn research into implementation by choosing a folder after the synthesis; Codey carries only a bounded Research Brief into the Writer prompt
- Inspect a Research run through `Evidence`, `Sources`, `Graph`, and `Notes` drawer tabs instead of reading a flat receipt; Notes render saved Markdown as bounded note cards with source chips, while PDF page locators and search coverage appear inside the existing evidence/source views
- Save successful implementation and verification facts back into local research memory without copying source code into the vault
- Discuss, inspect, and edit inside one project conversation; files change only when requested
- Let the model read and modify files in a selected project folder
- Run allowed tests, builds, linters, and type checks, then feed results back
  to the model
- Show red/green diffs
- Show a compact task receipt after each run, such as `DONE · 2 files changed · checks passed · restore available`
- Write a bounded local run trace for each task, recording mode, provider,
  Router result, prompt digests, tool contract hashes, and fallback facts
  without storing raw prompts, chat transcripts, source code, webpages, or raw
  provider errors
- Restore snapshot changes without requiring Git
- Use Git when available, but not require it
- Retry with another model when one model fails
- Use already-open models as hidden advisors for chat, empty-project planning, and read-only project audits
- Use two open web models together: one writes, the other reviews; when no
  second model is available, use the writer model in a temporary self-review
  tab instead of skipping review entirely
- Use hidden task briefs so Writer and Reviewer share the same bounded intent
- Give Writer, hidden advisors, and Reviewer a bounded local Project Map before
  they inspect files
- Let the model request bounded lexical reference hints before changing a symbol
- Flag a newly introduced Python syntax error immediately after a successful
  replacement edit, without rolling back or pretending a check passed
- Continue long conversations quietly with an automatic factual summary and fresh model chat
- Reuse project commands that have already succeeded locally
- Remember recent successful changes only from verified local checks
- Resume the same chat after a Codey restart or model switch from one bounded
  factual handoff plus recent visible conversation context
- Continue follow-up Research or Project Hybrid work from the same bounded chat handoff, so prompts like "continue researching that plan" keep the prior context
- Keep non-Git diff and restore available across Codey restarts
- Keep explicit learning signals in a local Ghost inbox first; only accepted
  typed preferences can become short neutral `Local Context` for normal chat
  and read-only planning
- Learn clear chat style preferences after a turn in a fresh provider tab,
  without typing extractor prompts into the current chat; `ghost disable` stops
  future learning
- Carry bounded continuity from accepted memory, short task focus, run ledgers,
  and Research note titles/structured `open_questions` without storing full transcripts,
  source files, Research bodies, or webpage text
- Inspect, export, delete, reset, or disable local Ghost state with
  topbar `... -> Local context` or `python -m codey ghost ...`
- Quietly maintain Ghost state after successful tasks with local health checks,
  due decay, continuity refresh, and event compaction; no web model, shell, UI
  change, or user-facing sleep control is involved
- Keep a bounded local work queue for saved follow-ups; strict continuation
  prompts like "continue" can claim one queued item, run it through Research,
  Writer, or Review, and mark it done only with local proof
- Turn structured Research `open_questions` and supported concept gaps into saved
  research follow-ups without starting background web searches
- Maintain a bounded local Affinity Index that links accepted preferences,
  task types, projects, research concepts, and provider outcome kinds for
  low-risk ordering only; it is not evidence, permission, or automation
- Recover changed composer controls through bounded local discovery or a
  healthy sibling model, then verify, promote, and roll back the local bundle
- Recover one bounded web-chat state rule from boolean-only evidence when a
  verified control bundle is not enough; provider-specific completion signals
  stay isolated inside each adapter
- Hand an interrupted project Writer to a healthy sibling model from bounded
  local checkpoint facts, with at most two switches and no repeated uncertain
  submission to the failed model
- Keep a small local provider-health circuit so rate limits, login pages, and
  repeated structural failures do not keep receiving hidden work
- Repair broken provider adapter code in a sandboxed background worker when
  bounded control and Flow recovery are not enough, then verify the candidate
  with an isolated worker canary before enabling it
- Stop a running provider wait, review, recovery, or test command promptly
- Give longer timeouts to full verification suites while keeping quick commands
  bounded
- Preserve both the beginning and end of long command output
- Tell the model the exact next `read_file` JSON call when a large file read
  returns only one page
- Suggest narrowing a literal grep when search results hit the match limit
- Fold obvious dependency stack frames from Python and Node command output so
  user-code failures survive the existing output budget
- Render assistant replies with quiet Markdown basics and copy buttons on code
  blocks, without syntax highlighting or new colors
- Recover the active run, approval, or teaching prompt after a UI reconnect
- Prevent uncertain provider submissions from being sent twice
- Record compact provider failure diagnostics for debugging web-page breakage
- Launch Edge by default, fall back to Chrome when needed, and keep the local
  HTTP UI available if the native WebView window cannot open

---

## Supported Models

| Model | Status |
|---|---|
| DeepSeek Web | Tested |
| Xiaomi MiMo | Tested |
| StepFun Chat | Tested |
| Qwen Studio | Tested |
| GLM | Tested |
| Local OpenAI-compatible | Optional; configure endpoint/model in Codey |

Codey uses browser automation, so websites may break after UI changes. The current design keeps provider-specific code isolated so those adapters can be repaired without changing the agent core.

If a provider page changes, Codey first attempts the bounded recovery above. When it still cannot identify a control safely, it pauses and asks you to click that control once. It stores only the latest verified control record, never the page DOM or your conversation, so the main workflow stays quiet.

Recovered controls are committed only after the original message is submitted
once and a new answer is read. The first successful bundle remains provisional;
the next natural success promotes it, while explicit repeated control failures
restore the previous bundle. Normal healthy sends do not call sibling models.

The same recovery bundle can contain one bounded Flow Recipe made only from
fixed boolean facts such as response stability and a verified stop-state
transition. Recipes cannot contain selectors, JavaScript, URLs, arbitrary
clicks, page text, or project data. Provider-specific completion logic remains
inside the individual adapters, and Codey does not promote a learned Flow rule
unless the current boolean facts prove it.

If a project Writer fails with an explicit provider-page error, Codey can move
the unfinished task to a healthy sibling model. The new Writer starts a clean
conversation and receives only the original task plus bounded local checkpoint
facts such as changed-file hashes and still-valid checks. Provider health stores
only counters, timestamps, and failure kinds; it never stores page text, URLs,
prompts, source code, cookies, or chat content. Cooled-down providers receive one
data-free canary only when they are about to handle real work again.

If a provider adapter itself breaks after a larger website change, Codey can
queue a background adapter self-repair. The repair path runs in a separate
Python process, asks a healthy helper model to modify only the broken provider's
adapter files in a temporary sandbox, validates the candidate with policy
checks, static checks, provider unit tests, and a neutral marker canary, then
loads the candidate only through a child Provider worker. The worker uses a
fresh background tab in the same logged-in Codey browser profile, so it does
not need to copy cookies or block your current task. Candidates start as
provisional, become active only after natural successes, and roll back after
repeated structural failures. Core files such as `agent.py`, `task_runner.py`,
`tool_runtime.py`, `server.py`, and the recovery/safety modules are not
self-modified by this v1.

---

## Research

`Research` is Codey's research work loop. It is not a separate app and it is not
automatic background browsing. You explicitly click the `Research` token in the
composer context when you want Codey to search, read sources, and write local
evidence notes.

The main screen stays the same:

```text
Choose folder · Research
```

- `Choose folder` attaches the current chat to a project.
- `Research` enables a research run for the current message.
- The provider picker below the composer chooses the active provider; choosing `Local` opens the
  local endpoint configuration popover.

Research can use web providers or `Local`. Search, page opening, URL policy,
note writes, restore, and evidence checks are always local Codey tools. Models
do not get hidden network access. A final synthesis can cite only sources that
Codey actually opened in the run. Research providers are also asked to choose
exactly one local JSON tool per turn; if a model emits several actions at once,
Codey treats that as a protocol error and asks it to retry.

In 0.4.0, Codey also builds a deterministic Research object record after the
run. It projects the existing ledger and report review into question, source,
evidence, claim, assumption, and relation objects. The record is conservative:
only matching opened-source evidence can attach to a claim. Claim `status`
only says `evidence_backed`, `unsupported`, or `assumption`; support,
refutation, and limits are expressed by relation kind. Search results are not
evidence, contradicting or unknown-stance evidence cannot support conclusions,
and the UI/SSE payload stays unchanged.

In 0.4.1, Codey quietly persists each Research object record into a bounded
local evidence ledger. The ledger keeps durable source, evidence, claim,
assumption, relation, locator, and count refs for later proof checks, without
saving raw prompts, raw model responses, full source text, raw URLs, or raw
absolute paths. It validates that kept records still point to existing ledger
entries on write and load, and load-time schema validation rejects unknown raw
fields, orphan entries, non-canonical scalar values, and locator/source
mismatches. Candidate writes must pass the same canonical checks before they
touch disk, so malformed records cannot poison future proof material. This does
not add UI or change Research prompts/tool results.

In 0.4.2, queued Research and open-question work items complete only after a
deterministic proof review passes. The review checks answer coverage against
the queued question, citation presence, opened-source evidence, locator/source
consistency, support relations, assumptions, and counter/limitation handling.
Queued proof checks bind coverage to the saved work-item title, and required
claims only count as supported when their own evidence refs are
`evidence_backed` and matched by a `supports` relation.
It writes only a bounded `research_proof:<digest>` summary, queued-question
digest, and planner-signal counts into Run Trace. Ordinary manual Research is
not blocked by this gate, and the UI/SSE payload, Research prompt, tool schema,
and model-visible tool results remain unchanged.

In 0.4.3, Codey adds a source connector boundary, a deterministic ResearchPlan
dry-run, and connector-aware Research search for PubMed/arXiv. The built-in
registry ships recorded/local fixtures for `local_file`, `csv_tsv`,
`json_file`, `arxiv`, and `pubmed`; `openalex` is deferred and `rss` is
optional, so they do not count as shipped connectors. Connector hits are
locator candidates, not evidence: only a fetched/opened source can later become
evidence through the existing ledger. The planner uses proof-review gaps and
connector metadata to choose bounded source preferences, for example PubMed for
medical or life-science questions and arXiv for papers or preprints. Production
Research exposes a controller-level action surface:
`web_search`/`open_result`/`reopen_source`/`open_hit`/`source_search`.
Those actions compile to the same runtime open/fetch path, so PubMed/arXiv
details stay inside Codey while the model sees unambiguous IDs instead of
overloaded `open_url` shapes. Run Trace stores only bounded dry-run summaries
without raw prompt text, source bodies, raw URLs, or raw absolute paths, and it
records the model-visible controller action hash separately from the compiled
runtime tool hash. PubMed/arXiv API queries are built from one shared safe
query boundary that masks high-confidence secret marker/value windows such as
`api key ...` or `api key is ...`, plus longer connector phrases such as
`password is equal to ...`, `password is set to ...`,
`password is configured as ...`, `client secret known as ...`,
`api key called ...`, over-padded or punctuation-separated connector phrases
such as `password is configured as known as called ...` and
`password - is - configured - as - known - as - called - ...`, and Chinese
windows such as `密码 是 ...` or `密钥等于 ...`. Common token and cookie markers
such as `access_token ...` and `passphrase ...` are masked too. Bare contextual
markers such as `token`, `cookie`, and `jwt` only mask value-shaped followers
like `token abcdef`, so queries such as `token classification benchmark` keep
their domain terms. Multi-word values after explicit secret markers are bounded
by domain terms. Cleaned domain terms can still drive connectors, while URLs,
local paths, and path-like slash tokens are dropped; connector lookup is skipped
when no safe terms remain. Live connector routing and request assembly reuse
that single safe query instead of deriving terms repeatedly from raw text.
Browser-backed Research search explicitly reuses one dedicated Research
profile/port for ordinary runs, while direct `BrowserSearchProvider()`
construction stays isolated by default, CDP attach/port waits stay bounded at
20 seconds, and cancellation is not retried as a launch/navigation failure.
Recorded PubMed/arXiv fixtures and recorded fetches validate both connector
host and source-ID shape, connector result digests derive from safe query
terms, `SourceHit` audit payloads filter secret-looking refs and allow-list
scalar fields, `FetchedSource` audit payloads allow-list fetched scalar fields,
connector catalog id/kind values reject secret-looking or non-canonical codes,
catalog/result warning and error payloads filter secret-looking codes, and
proof-complete no-op plans stay warning-free.
Bounded connector fallback errors and adjacent evidence/proof reason or warning
codes are recorded in Run Trace without raw request data while preserving safe
audit codes such as `token_budget_exceeded` and `authorization_required`; live
transport metadata uses a neutral tool name and User-Agent.
Research JSON tool calls must be exactly one plain JSON object with only
top-level `tool` plus `args`; hidden `name`, top-level-argument, extra-field,
extra-object, array, fenced-block, or prose-wrapper shapes are protocol errors.
The planner and live connector wrapper share one domain-routing table with
RAG/NLP/retrieval/benchmark terms, enforce registry availability/capability
flags, keep safe scientific terms such as `JAK/STAT`, drop CamelCase
path-like slash tokens such as `Docs/ADR/Plan`, avoid treating `secreted` or
`secretion` as secret markers, and use a strict connector deadline. Qwen waits
for an interactive, non-generating
composer before filling it and never repeats a whole send after a slow
post-click response confirmation; browser PDF requests also use neutral
transport metadata.

In 0.2.20, production Research uses a thin controller instead of exposing the
full tool menu every turn. Codey reads the current Research ledger, shows only
reasonable allowed tools for that turn, and assigns stable run-global IDs:
`result_id` for search results, `source_id` for opened sources, and `hit_id` for
source_search locators. Models can choose IDs instead of hand-copying URLs,
PDF pages, or HTML offsets. This is not a hard linear state machine: local
memory search and web search remain available so the model can go back for
counter-evidence or better sources. The existing typed tool contract and report
quality gate still decide what actually executes and what can be saved.

In 0.2.19, browser-backed Research search and page opening run in a separate
Research browser profile and CDP port instead of sharing the provider chat
browser. This keeps Bing/result/article tabs away from DeepSeek, MiMo, StepFun,
Qwen, and GLM chat tabs, avoiding web-provider stalls where a model reply
appeared only after stopping Codey. Codey also retries brief `Page.content`
navigation races and shows `Turn N (done)` when a final-report attempt is
rejected by quality review or private evidence review.

In 0.2.18, Research JSON calls are checked against a typed local contract before
they execute. Codey now distinguishes missing JSON, unknown tools, too many tool
calls, invalid arguments, direct prose answers, and suspected use of a chat
website's own search. The repair prompt names the specific problem and gives one
copyable JSON shape. Final reports must use `done`; Codey saves the synthesis
after the report passes quality review.

Provider fit matters. Codey does not route automatically by role yet; choose the
provider that fits the current job:

| Provider | Best Fit | Current Caution |
|---|---|---|
| DeepSeek / Qwen / GLM | General coding, review, and Research | Daily web-use limits can interrupt long runs |
| MiMo | Coding/editing when stronger providers are limited; small Research runs after the one-tool boundary | Higher variance for strict JSON-tool Research; Codey waits for MiMo's response footer before the next send, but longer research still works best on DeepSeek, Qwen, StepFun, or Local |
| StepFun | Evidence-backed Research and local JSON-tool probes | The adapter now waits for StepFun's response footer before the next send; not recommended as the main writer for fresh projects yet |
| Local | Private/offline runs and quota fallback | Quality depends on your local model; Gemma4-12B passed fixture probes, but heavier prompts can stress JSON discipline |

MiniMax was also probed and not selected because its Agent page ignored the
local JSON-tool protocol on the first probe and used its own web/agent behavior
instead.

The manual Deep Research A/B harness has now checked DeepSeek, StepFun, Qwen,
and a local Gemma4-12B endpoint. The consistent result is that deterministic
`source_search` inside already-opened sources is useful enough for production.
The heavier `deep_core` plan/coverage prompt remains an A/B experiment, not
default production behavior.

MiMo was retested after adding the one-tool Research boundary. A fresh-tab
`long-official-doc/source_search` run completed in 10 turns, used
`source_search`, opened the target offset, saved exact evidence, and passed the
report quality gate. Earlier MiMo probes without that boundary still emitted
multiple search calls, so the documentation treats this as improved Research
discipline rather than a role router.

MiMo was retested again in 0.2.18 after Codey added typed tool-contract repairs
and a MiMo-local response-footer wait. A continuous long-message submit probe
completed two sends without timeout, and the same `long-official-doc/source_search`
fixture completed with `done=True` in 9 turns. Qwen stayed format-clean in the
same source_search fixture, but still spent its 10-turn budget on intermediate
note writing instead of reaching `done`.

The manual A/B harness has a `thin_gate` probe arm that informed the 0.2.20
production controller work. It appends state-aware allowed tools plus stable
`result_id` and `source_id` choices. In a live MiMo
`long-official-doc/thin_gate` probe, MiMo completed with `done=True`,
`quality_score=11`, zero protocol repairs, and four ID rewrites in eight turns.
That supported the narrow 0.2.20 direction: allowed-tools and stable IDs, not a
hard linear controller or Deep Research Core.

Production Research can now call `source_search` after `open_url`. It searches
only inside sources Codey already opened and returns locator previews, not
evidence. For HTML, Codey asks the model to open the returned offset before
citing. For PDF page-specific evidence, the existing hard gate still applies:
the model must call `open_url pages="N"` before it can cite `[n p.N]` or save
evidence for that page.

In 0.2.4, Research keeps an Evidence Ledger and applies a deterministic report
quality gate before saving the final synthesis. The report must include:

- `Conclusion`
- `Key evidence`
- `Counter-evidence / limitations`
- `Source quality`
- `Search coverage`
- `Sources`

Numbered citations such as `[1]` must map to final URLs opened by Codey during
that run. Each cited source must also have at least one saved evidence snippet
copied from the opened page text. Search-result URLs do not count as evidence
until Codey opens them.

Final `done` replies now pass through a narrow citation compiler first: it
renumbers existing source IDs and renders `来源` from evidence-backed opened
URLs when references have a reliable source-id or parsed source-map binding,
keeps source-id rewrites separate from old numeric source-table remaps, and
leaves non-citation bracket text alone. It can normalize numeric drift when the
parsed old source rows all resolve to one canonical URL, including repeated
rows for the same source, but it does not add new support, guess ambiguous
citation mappings, leak internal source IDs, or relax the quality gate.
Source-id leakage checks apply to the final report body and metadata sections;
source titles in `来源` may still contain literal text such as `[S1]`, and
ordinary prose tokens like `s1` are not treated as internal IDs.

PDF is part of the same `open_url` source intake. There is no `open_pdf` tool,
PDF mode, or extra button. When a URL points to a text PDF, Codey reads bounded
pages by default, records page metadata in the Evidence Ledger, and lets the
report cite page-specific evidence such as `[1 p.4]`. That page citation passes
only if Codey actually read the page and saved a snippet from that page. Scanned,
oversized, or extraction-failing PDFs are neutral `SKIPPED` results and do not
become opened sources.

The validator accepts common report formatting such as `1. Conclusion`,
`一、结论`, and source rows written as `[1] [Title](https://...)`, but it does
not relax source provenance or snippet matching. Explicit URL citations must
still match pages Codey opened as final URLs. Bare site-domain mentions in
source-quality text are more natural: opening `docs.python.org` allows a
quality note to say `python.org`, while opening `python.org` does not let the
report claim `docs.python.org`.

When a result points to a source Codey cannot read, such as a scanned or very
large PDF, Research marks that tool result as `SKIPPED` instead of a hard
failure and continues with other readable sources. If a model writes a
paraphrased evidence excerpt for an opened page or PDF page, Codey replaces it
with an exact opened-source snippet and records the note with a warning.

The flow is:

```text
Chat about an idea
-> click Research and ask the research question
-> Codey searches, opens pages, records evidence, and saves a cited synthesis
-> choose a folder
-> Codey injects a bounded Research Brief into the project Writer
-> successful implementation/verification can be remembered as project facts
```

The Research drawer has four lightweight tabs:

- `Evidence`: claims, snippets, PDF page locators, counterpoints, quality warnings, and search coverage
- `Sources`: citation map, source titles, final URLs, quality hints, and PDF pages read/truncation metadata
- `Graph`: a bounded unified graph with virtual concepts at the top, the current synthesis/report and related notes in the middle, and source URLs at depth 3; open questions stay text on concept nodes, marked "unproven; not facts"
- `Notes`: readable note cards with bounded Markdown previews, source chips,
  and restore state

Coverage stays as supporting audit detail inside `Evidence`, rather than a
first-level concept. `Graph` is a presentation read model, not a new database
or a full-vault knowledge map: declared concept relations stay virtual, evidence
links stay note/source links, and tag edges only connect visible notes to
concepts.

The vault is stored under Codey's local state directory and is implemented as
Markdown notes plus a rebuildable SQLite FTS index. Project source code is not
copied into the vault; implementation notes record what changed, why, what was
checked, and which research synthesis or decision it relates to. The Project
Writer receives a bounded brief with key conclusions, citation map, evidence
items, counterpoints, and source-quality risks, not the whole vault.

---

## Hidden MoA Advice

MoA (Mixture of Agents) is Codey's invisible advisory layer. It adds no button, mode, or dashboard. In New Chat, the selected owner model drafts first, up to two other open models privately critique or supplement it, and the owner produces the one answer you see. Empty or placeholder-only projects use the same owner-first pattern for planning. In an existing project, advisors instead perform bounded read-only audits before the Writer acts.

Advisors cannot edit files, run commands, request Shell approval, access anything outside the selected project, or read sensitive and excluded paths. Their reports are suggestions: the Writer must verify them against real files. Advisor failures quietly fall back to the owner model.

This hidden MoA layer is separate from the post-change second-model Diff Review described below. MoA helps before or during the owner's reasoning; Diff Review checks the actual final changes.

---

## Two-Model Assistance

One AI model can write code, but it can also miss small mistakes. Two models make the loop steadier: one model focuses on building, and another model looks over the changed code like a second pair of eyes.

You do not need to learn a new mode. If you open two supported AI pages in the Codey browser, Codey can automatically use them together:

- The model you select in Codey is the writer.
- Another open supported model becomes the reviewer.
- The writer reads files, edits code, and runs tests.
- The reviewer does not touch your files. It reads the diff plus a short bounded
  impact map of changed symbols, likely callers, and related tests, then points
  out concrete problems.
- If the reviewer approves, Codey finishes.
- If the reviewer finds a real issue, Codey asks the writer to repair it once more.

The reviewer runs only after the selected model actually changes a file. Project
questions and read-only analysis return the selected model's answer directly,
without turning a conversation into a code review.

If no different reviewer model is available, Codey can still run a same-model
self-review in a temporary fresh tab. This is not an independent second
opinion, but it asks the writer model to inspect the final diff again with the
same bounded Review prompt, Impact Map, Verification Map, and evidence. If that
self-review also fails, Codey quietly keeps the single-model result.

In plain words: two different models are best, because it is like a second
teacher checking the work. One model can still do a self-review, which is like
asking the same teacher to read it again carefully. If review is unavailable,
Codey falls back to the original result. No group chat, no extra switch, no new
concepts on the main screen.

---

## Quick Start

### 1. Install dependencies

```powershell
pip install -r requirements.txt
```

### 2. Start Codey

```powershell
python -m codey
```

Codey opens a local UI at `http://127.0.0.1:<port>/`.

### 3. Log in once

When Codey opens the selected AI website in a dedicated browser profile, log in manually once.

The browser profile is separate from your normal daily browser profile:

```text
C:\Users\<you>\.codey\edge-profile
C:\Users\<you>\.codey\chrome-profile
```

You can leave that model browser window open. Restarting the Codey UI does not close it; the next run will quietly reconnect to the existing CDP browser and tab when possible.

### 4. Pick a project and ask

Example:

```text
Create a small Python snake game in one file. Make it runnable with python snake.py.
```

Codey will ask the web AI for structured tool calls, apply edits locally, and show what changed.

You can also start in **New Chat** with no project access, discuss a plan, then
click the composer project context (`Choose folder`) to attach that same chat to
a folder. If there is a draft in the composer, clicking the same context keeps
the draft and sends it after the folder is chosen. If you only want a general
conversation with no project access, keep using **New Chat**.

To research before coding, click the `Research` token in the same composer
context, then ask for the sources, comparison, market scan, API notes, or design
background you need. Codey writes local research notes and a cited synthesis
with evidence, counterpoints, source quality, and search coverage. When you
then choose a folder, the Writer receives a bounded Research Brief instead of
the whole vault.

To use a local model, select `Local` from the model menu. Codey opens a compact
configuration popover for the OpenAI-compatible base URL, model id, and optional
API key. Leaving the key blank keeps any saved key; entering a new key replaces
the saved key when you click `Connect`.

When a task finishes, Codey summarizes the local facts in one quiet line:

```text
DONE · 2 files changed · checks passed · restore available        View diff
```

`View diff` opens the right-side changes drawer for the detailed red/green diff.

---

## Project-local Config

Projects may optionally include `.codey/config.json` to declare local facts and
preferences such as verification command candidates, scan ignored path
prefixes, and a smaller Project Map budget. Codey never creates this file
automatically.

Configured commands are suggestions, not permissions. They still must pass the
normal executable, cwd-in-project, and `tool_runtime` run allowlist checks; shell
approval and safe-path guards are unchanged. Provider preferences are parsed as
future hints only and do not override the provider you select.

---

## Safety Model

Codey is not an unrestricted shell.

- File operations are limited to the selected project folder.
- Normal edits are shown as diffs.
- Snapshot restore works even without Git.
- Git integration is optional.
- Shell commands require approval; setup/install approvals show risk notes,
  pass read-only local setup facts back to the writer, and include guarded
  follow-up hints after approval.
- The UI keeps failure recovery simple: `ERROR · Could not send the message  Retry`.

You should still review diffs before trusting generated code.

---

## Git Is Optional

Codey is designed to work before a beginner understands Git.

| Environment | Behavior |
|---|---|
| No Git installed | create/edit files, show red/green diffs, save local snapshots, restore changes |
| Git installed, not a repository | same local diff flow, with a path to initialize Git later |
| Git repository | full Git diff, commit workflow, stronger history and rollback |

Git is an upgrade path, not an entry requirement.

---

## Self-Bootstrap Proof

Codey has been tested repairing broken copies of itself using DeepSeek, MiMo, StepFun, and Qwen.

Each model:

1. ran failing tests,
2. read Codey source files,
3. edited the broken code,
4. reran tests,
5. reached a green state.

See [BOOTSTRAP_PROOF.md](BOOTSTRAP_PROOF.md).

The current release also includes [TEST_REPORT.md](TEST_REPORT.md), which records the latest single-model, two-model, MoA, and self-bootstrap smoke results.

This does not prove Codey will never break. It proves the core repair loop exists: when Codey breaks in a testable way, it can use connected web AI, local tools, diff, restore, and tests to help repair itself.

## End-to-end tests

The real Edge UI flow can be replayed with a deterministic test provider. The
test covers project selection, provider switching, SSE, file edits, test
execution, review, task receipts, diff, and snapshot restore:

```powershell
python -B tools/ui_e2e.py --artifacts .e2e-artifacts --json
```

With the supported web model pages logged in through Edge CDP, run the
real-provider matrix below. Every result is independently checked with a functional
assertion and unittest after the agent finishes:

```powershell
python -B tools/live_smoke.py --provider all --case edit --port 9222 --max-turns 10 --json
```

The explicit MoA snake flow is kept under `tests/` because it is a real smoke
test, not a general tool. It writes its checkpoints and timing log inside the
target project under `.codey/smoke/moa-snake-flow`:

```powershell
python -B tests\moa_snake_flow.py --project E:\snake --reset --json
```

---

## Example Tasks

Generate a small program:

```text
Write a complete classic Snake game in pygame as a single file snake.py.
The file must run with: python snake.py
```

Fix a bug:

```text
There is a file buggy.py with a subtle bug. Read it, fix the bug,
write the corrected version back to buggy.py, then run the test.
```

---

## CLI

You can also run Codey without the web control panel:

```powershell
# Single chat message
python -m codey chat "Explain Python's GIL in one sentence"

# Use Qwen
python -m codey chat --provider qwen "Explain Python's GIL in one sentence"

# Run the agent directly
python -m codey agent --provider qwen --project E:\my-project --max-turns 10 "Fix the failing tests"

# Emit machine-readable JSONL events for scripts or CI wrappers
python -m codey agent --json --provider qwen --project E:\my-project "Fix the failing tests"
```

---

## Architecture

```text
UI / CLI
   |
Server / Orchestrator
   |
Agent Runtime -- JsonToolCodec
   |
ChatProvider -- DeepSeekWebProvider
             -- MimoWebProvider
             -- StepFunWebProvider
             -- QwenWebProvider
             -- GlmWebProvider
             -- LocalOpenAIProvider
   |
Browser Session + provider DOM driver
```

`agent.py` only knows about `ChatProvider`, `ProtocolCodec`, and tool calls. Browser automation and website selectors live in provider-specific adapters.

---

## Project Structure

```text
codey/
  agent.py                  provider-independent agent runtime
  models.py                 shared tool-call and protocol data models
  cancellation.py           shared task-local cancellation and process cleanup
  events.py                 structured run events and log rendering
  text_budget.py            bounded head-and-tail output clipping
  bounded_scan.py           shared bounded local file traversal
  scan_report.py            compact scan omission facts and coverage rendering
  tool_definition.py        internal coding tool metadata and render hints
  capabilities.py           read-only built-in capability boundary registry
  builtin_profiles.py       read-only built-in default-profile catalog
  permission_profiles.py    internal tool/context permission profiles
  action_policy.py          monotonic local action allow/ask/deny guards
  context_source.py         named bounded prompt context assembly
  prompt_envelope.py        prompt section envelopes and fail-open trace sink
  tool_runtime.py           local tools and structured outcomes
  execution_evidence.py     bounded in-memory execution fact ledger
  run_ledger.py             append-only project-task run fact ledger
  run_ledger_projection.py  read-only run ledger summaries and receipt projection
  run_trace.py              bounded per-run audit manifest sidecars
  run_details.py            bounded user-facing run explanation projection
  references.py             bounded lexical reference hints
  change_set.py             structured diff files, hunks, and rename/copy facts
  changed_symbols.py        lexical changed-symbol extraction from visible diffs
  project_map.py            deterministic bounded project orientation
  project_config.py         strict project-local config facts and warnings
  project_task_context.py   project facts, map, checkpoint, and verification context
  ghost/                    Ghost signal extraction, memory state, continuity, routing, local work queue, affinity ledger, and local context control surface
  knowledge/                local Markdown vault, FTS index, restore, and Research Briefs
  research/                 Research controller/runner, isolated web/source search tools, evidence ledger, object model, report/proof quality gates
  verification_map.py       bounded review-time verification candidates
  review_impact_map.py      review-only changed-symbol caller/test hints
  change_brief.py           hidden task intent brief
  review_coordinator.py     bounded diff review lifecycle
  task_runner.py            task, conversation, review, and receipt orchestration
  headless_runner.py        TaskRunner-backed JSONL entry point for scripts/CI
  browser.py                Chromium CDP connection helpers
  browser_worker.py         Playwright thread scheduler
  changes.py                Git and snapshot diff / restore support
  local_store.py            shared local data root and atomic JSON writes
  managed_outputs.py        run-scoped handles for truncated command output
  project_facts.py          facts verified by successful local runs
  work_checkpoint.py        durable facts for unfinished execution
  conversation_store.py     bounded factual conversation persistence
  provider_profiles.json    versioned selectors for supported model pages
  provider_profiles.py      validated profile loader
  provider_discovery.py     bounded DOM candidate discovery and scoring
  provider_controls.py      verified recovery, learning, and human teaching
  provider_flow.py          bounded boolean web-chat state rules
  provider_revival.py       atomic control bundles, promotion, and rollback
  provider_submission.py    shared one-shot remote submission boundary
  provider_send_loop.py     shared send-loop lifecycle helpers for web providers
  provider_timeouts.py      shared provider deadline and navigation timeout helpers
  provider_capabilities.py  static provider fit hints for fallback ordering
  provider_supervisor.py    passive health circuit, Writer selection, and canary
  adapter_overrides.py      local adapter candidates, promotion, and rollback
  adapter_repair.py         sandboxed provider adapter repair runner
  repair_policy.py          strict adapter repair file and code policy
  repair_sandbox.py         temporary source copy for adapter repair
  repair_journal.py         bounded local adapter repair journal
  self_repair.py            deduplicated background repair queue
  self_repair_worker.py     repair subprocess entry point and helper selection
  provider_worker.py        parent-side isolated adapter worker wrapper
  provider_worker_child.py  child process adapter runner
  profile_doctor.py         one-shot sanitized candidate selection
  json_tool_reply.py        tolerant final JSON tool-reply detection and repair
  web_clipboard.py          bounded copy-action clipboard transaction helper
  deepseek.py               DeepSeek page driver
  mimo.py                   MiMo page driver
  stepfun.py                StepFun page driver
  qwen.py                   Qwen page driver
  glm.py                    GLM page driver
  provider_diagnostics.py   compact provider failure records
  receipt.py                task completion receipt builder
  protocols/
    json_codec.py           JSON-only tool protocol
  providers/
    registry.py             provider registry and sibling-tab borrowing
    local_openai.py         OpenAI-compatible local model provider
    *_web.py                provider adapters
  server.py                 local HTTP + SSE transport and runtime state
  web/
    index.html              UI core: state, SSE, composer, boot
    assets/                 zero-build CSS tokens/styles and plain-script UI modules
```

---

## Limitations

- Web AI pages can change and break automation.
- Model quality varies.
- Web models may produce verbose or imperfect code.
- Codey is a local developer tool, not a security sandbox.
- You still need to review changes before keeping them.

---

## License

Codey is released under the GNU General Public License version 2 only
(`GPL-2.0-only`). See [LICENSE](LICENSE).
