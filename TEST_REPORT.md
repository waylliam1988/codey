# Codey 0.1.43 Test Report

## 0.1.43 Quiet UI Persistence and Sidebar Polish

UI state persistence now separates the SSE hot path from discrete user actions.
`addToSession()` still records every streamed event, but its full localStorage
serialization and `/api/ui_state` POST are debounced. User actions such as
switching chats, creating chats, renaming, deleting, clearing, and provider
selection flush immediately. Terminal task events also flush immediately so the
completed receipt is durable without waiting for the debounce timer.

Sidebar rename now uses inline inputs instead of native `prompt()`. Destructive
actions use a quiet two-step confirmation inside the existing monochrome menu
instead of native `confirm()`. Consecutive read-only tool rows fold at render
time only after a second same-kind row appears; a single read/search/list or
reference row remains visible, while edit, run, shell, and error rows stay
expanded.

Validation:

```text
UI focused unittest: 40 tests OK
Full unittest: 826 tests OK
Full pytest: 834 passed, 67 subtests passed
Ruff, compileall, and git diff --check: passed
```

## Internal Writer Failover Refactor

Writer provider takeover is now isolated in `codey/writer_failover.py`.
`TaskRunner` still owns prompts, verification, review, diffs, receipts, and
project facts; the new runner only coordinates Writer attempts, provider
switches, shared turn budget, canary checks, checkpoint refresh, and Stop
priority. Initial Writer execution and Review repair reuse the same runner
instance so the switch budget and tried-provider set remain shared.

This is an internal maintainability refactor. It does not change the provider
protocol or prompt contract.

Validation:

```text
Focused failover/server/work-checkpoint tests: 107 tests OK
Full pytest: 827 passed, 67 subtests passed
Ruff, py_compile, and git diff --check: passed
```

## 0.1.42 Broader Checks and Quiet Markdown

The controlled `run` allowlist now accepts additional verification commands:
Ruff check and format-check, mypy, safe make targets, Bun test / allowed scripts,
and safe Deno test/lint/check/fmt forms. Mutating and installing variants remain
blocked, including Ruff fix/output-file forms, unguarded Ruff format,
`mypy --install-types`, deploy-style make targets, `bun install`, and
`deno run`.

Suite-style checks now receive a 300-second timeout, while quick commands keep
the existing 90-second timeout. Timeout output explicitly says it is a timeout,
not a test failure, and asks the Writer to rerun a smaller subset instead of
guessing a fix. Literal grep results now add a narrowing hint when they hit the
match cap.

The UI now renders assistant replies with a minimal monochrome Markdown subset:
code blocks, inline code, bold text, simple headings, and simple lists. Code
blocks get a quiet copy button that reuses the existing clipboard helper. The
implementation escapes text before applying inline markup, uses `textContent`
for fenced code, and adds no syntax highlighter or new color palette.

Validation:

```text
Full unittest: 810 tests OK
Full pytest: 818 passed, 67 subtests passed
ruff check .: passed
compileall: passed
```

## 0.1.41 Smart Pagination Hint

Paged `read_file` results now include a concrete next-page JSON call when more
content remains. The existing complete-line paging, `next offset` text, page
metadata format, and truncation semantics remain intact. The added hint uses
the same path, next offset, and effective limit:

```text
next call: {"tool":"read_file","args":{"path":"app.py","offset":301,"limit":300}}
```

The hint is omitted on the final page and is generated with JSON escaping so
Windows-style paths and other escaped characters remain valid JSON. This change
does not touch providers, Agent orchestration, file contents, or verification
logic.

Validation:

```text
Focused runtime/agent/protocol tests: 168 tests OK
Full unittest: 799 tests OK
Full pytest: 807 passed, 67 subtests passed
Ruff, compileall, and git diff --check: passed
```

## 0.1.40 Bounded Stacktrace Pruning

Controlled `run` output now folds obvious dependency stack frames before the
existing `clip_middle()` budget is applied. The pruning is deterministic,
line-oriented, and local to display text: it does not change command execution,
exit codes, `ok`, `changed`, or `truncated` semantics.

Python pruning recognizes explicit traceback frames under `site-packages`,
`dist-packages`, `.venv`, and `venv`, and folds the immediately following
dependency source line with the frame. Node pruning recognizes only explicit
`at ...` stack entries whose parsed location ends with `:line:column` and lives
under `node_modules` or `.pnpm`. Project frames, assertion lines, exception
messages, test names, and ordinary logs are preserved. If no dependency stack
frame is found, the output is returned byte-for-byte unchanged.

No live web-provider smoke was required for this release because the change is
pure local text post-processing after a controlled command completes and before
the existing output clipping step.

Validation:

```text
Focused text-budget/runtime tests: 59 tests OK
Full unittest: 798 tests OK
Full pytest: 806 passed, 67 subtests passed
Ruff, compileall, and git diff --check: passed
```

## Post-0.1.40 Incomplete Refactor Hint Probe

`tests/manual/refactor_hint_ab.py` was added as a probe-only A/B harness. The
hint arm monkeypatches successful replacement edits inside the script only:
after a narrow identifier rename, it runs a bounded lexical scan for the old
symbol in other Python/JS/TS source files and appends only a file-count note.
No production code, prompt, protocol, version, or runtime behavior changed.

Live web A/B did not meet the retention threshold. DeepSeek, Qwen, and MiMo
all completed the explicit `python-function-rename` case correctly in both
arms with no missed callers. The shorter `implicit-function-rename` case
showed one positive Qwen sample, improving from 8 turns / 7 tool calls to
5 turns / 6 tool calls, but DeepSeek stayed neutral and MiMo regressed by one
tool call. Qwen's `public-string-control` case showed no over-rename of the
external string contract. Class rename samples for Qwen and DeepSeek were
neutral.

Conclusion: keep the manual probe for future larger-project refactor testing,
but do not promote the hint to production on the current evidence.

Validation:

```text
python -B tests\manual\refactor_hint_ab.py --self-test: passed
Ruff and py_compile for the probe: passed
```

## 0.1.39 MiMo Typing Transition Flow

MiMo now contributes an explicit three-state typing observation to the existing
bounded completion Flow. Deterministic tests cover true-to-false recovery,
thinking pauses, initial false without generation evidence, missing attributes,
DOM errors, built-in completion priority, unreadable final answers, provisional
promotion, rollback, Stop, and deadline propagation.

The live evidence probe stores only bounded booleans, lengths, timings, and
recovery status. It never stores prompt or reply text. All required scenarios
passed:

| Scenario | Result | Evidence |
|---|---|---|
| Short answer | passed | typing transition, stable completion, no later growth |
| Long code | passed | typing remained true during generation, then stable false |
| Deep thinking | passed | empty thinking interval stayed true; no premature completion |
| Forced Flow | passed | two distinct sends, `provisional -> active` |

Observed end-to-end durations were 12.41s, 31.83s, 29.81s, and 20.73s
respectively. The long-code probe was aligned with the production evaluator:
an initial false observation is not enough; completion requires the subsequent
stable non-empty samples used by the real Flow rule.

All browser-visible validation markers now use neutral `SESSION_CHECK_<random>`
values. Temporary DOM attributes, page globals, and clipboard sentinels likewise
contain no product name. Local-only module names, report paths, and environment
variables remain unchanged because web pages cannot observe them.

Final regression:

```text
Focused provider regression: 146 tests OK
Full unittest: 792 tests OK
Full pytest: 800 passed, 67 subtests passed
MiMo probe self-test: passed
Ruff, compileall, residual marker scan, and git diff --check: passed
```

## 0.1.38 Bounded Provider Flow Recovery

Provider recovery now includes one optional single-stage Flow Recipe made only
from fixed boolean observations. Deterministic tests cover schema rejection,
AND evaluation, generation-to-terminal transitions, bounded traces, assistance
suppression, transaction-local attribution, provisional promotion, persistent
failure rollback, profile invalidation, and safe degradation when terminal
evidence is absent. Production-component fault injection additionally covers:

- Qwen recovery when its built-in completion signal fails but a real
  `stop_visible -> stop_hidden` transition remains.
- no completion during a long thinking pause while stop remains visible.
- no guessed completion when the stop marker is also unavailable.
- one failure count per unreadable Flow-selected answer and rollback after the
  second independent structural failure.
- provisional promotion only after the next natural send proves the same rule.
- no MiMo or GLM completion recovery from text stability alone.

The four-provider Edge/CDP control-revival matrix used a temporary store and
in-memory selector faults. Every provider recovered its composer controls,
read both nonce replies, reused the bundle without invoking Doctor again, and
promoted `provisional -> active`:

| Target | First recovery | Persisted reuse | Result |
|---|---:|---:|---|
| DeepSeek | 32.48s | 4.70s | passed |
| MiMo | 21.77s | 12.31s | passed |
| Qwen | 46.92s | 6.95s | passed |
| GLM | 44.72s | 14.41s | passed |

A stricter Qwen live run forced the built-in completion check to remain false
while preserving real stop DOM observations. The first send completed through
the bounded Flow path in 64.34s; the second reused it in 6.06s and promoted the
bundle to active. The first helper could not provide a usable control choice,
so bounded sibling relay continued to MiMo and succeeded without recursion or
duplicate submission.

Final regression after the local fault-injection additions:

```text
Provider/Server focused unittest: 287 tests OK
Full unittest: 781 tests OK
Full pytest: 789 passed, 56 subtests passed
Ruff, compileall, and git diff --check: passed
```

The current completion Flow scope is intentionally narrow: Qwen has reliable
terminal evidence; MiMo and GLM do not yet. This release does not add arbitrary
browser actions, background probing, Python adapter self-modification, or a new
user-visible mode.

## 0.1.37 Python Syntax Regression Hint

A narrow production check compares Python replacement edits before and after
with `ast.parse()`. It emits a bounded navigation hint only when the
original file parsed successfully and the final edited content does not. The
edit remains written and successful; existing-invalid files, non-Python files,
valid edits, and files above the 128K-character parsing budget receive no hint.

The live A/B injected the same missing colon after the first successful target
edit. DeepSeek and Qwen ran baseline first; MiMo and GLM ran hint first to
reduce fixed-order bias. Every fault arm ended with valid syntax, the requested
change, and an independently passing unittest. Every valid-edit control passed
with zero generated or exposed hints.

| Provider | Turns baseline -> hint | Tools baseline -> hint | Runs baseline -> hint | Result |
|---|---:|---:|---:|---|
| DeepSeek | 7 -> 6 | 6 -> 5 | 2 -> 1 | avoided failed run |
| Qwen | 7 -> 6 | 7 -> 5 | 2 -> 1 | avoided failed run and extra read |
| MiMo | 8 -> 6 | 8 -> 6 | 2 -> 1 | avoided failed run and extra read |
| GLM | 7 -> 7 | 6 -> 6 | 2 -> 1 | failed run replaced by one read |

No provider reached max turns or hit a DOM, submission, rate-limit, retry, or
response-timeout failure. All four avoided one failed test run, three reduced
turns or total tool calls, and legal edits produced no false positives. This
meets the predeclared retention threshold for the narrow Python-only behavior;
it does not justify Ruff, JavaScript/TypeScript, LSP, automatic rollback, or
automatic command execution.

## 0.1.36 Provider Revival and Writer Takeover

This release adds atomic provider-control revival, passive provider health,
bounded half-open canaries, and checkpoint-based Writer takeover. Deterministic
tests cover typed failure boundaries, cooldown and restart behavior, Stop
priority, strict fresh-chat takeover, shared turn budgets, stale-check
invalidation, final Diff ownership across Writers, and Review-repair failover.
The four-provider live fault-injection matrix is recorded in the dedicated
section below.

## 0.1.35 Default Post-edit Verification

The default completion boundary now offers one exact trusted check after code
changes when candidate discovery proves a unique runnable command, compatible
ecosystem, and covering working directory. Discovery is bounded to successful
ProjectFacts or checkpoint commands and explicit pytest, npm, Cargo, and Go
configuration. Documentation-only changes, unavailable executables, ambiguous
candidates, and cross-ecosystem commands safely disable the default gate.

Agent regressions cover the per-edit-epoch reminder, proactive green-check
reuse, same-epoch failed-check non-repetition, latest-edit invalidation, exact
candidate matching, and checkpoint reuse. Receipt coverage confirms that a
successful same-ecosystem command cannot stand in for the selected candidate.
Policy tests cover manifest discovery, executable filtering, ecosystem
compatibility, closest monorepo scope, documentation skipping, and cwd
coverage. Candidate directory discovery has both directory and cumulative
entry budgets, skips dot directories and case-insensitive excluded directories,
and safely stops when either budget is exhausted. Production behavior was
first locked locally, then checked with the four-provider smoke recorded below.

Candidate manifests are refreshed from current disk state once per edit epoch
at the completion boundary and once before the final receipt. Historical
ProjectFacts commands are frozen at task start so an unrelated successful run
from the current task cannot promote itself into a trusted candidate. The
manual A/B current arm now calls production discovery, selection, exact-match,
and Agent gate code directly; it contains no duplicate policy or verification
monkeypatch.

Frozen historical and checkpoint commands that depend on manifests are also
revalidated: npm commands require the named current package script, Cargo
requires `Cargo.toml`, and Go requires `go.mod`. Python history remains valid
without a manifest because pytest, unittest, and py_compile can be legitimate
project checks without configuration files.

All verification manifests must be regular project files. Discovery and
historical-command revalidation reject symlinked `package.json`, `pytest.ini`,
`pyproject.toml`, `Cargo.toml`, and `go.mod`, so configuration cannot be
imported through a link to a file outside the selected project.
The read helper also proves the path is a regular file before `stat()` or
`read_text()`, preventing FIFO, socket, device, and directory manifests from
being opened.

Four-provider production smoke used the `python-pytest` current arm after all
policy fixes. Every provider changed only `limits.py`, passed the independent
check, ran the exact selected `python -m pytest` command after the latest edit,
and completed with zero wrong-run attempts:

- DeepSeek: 4 turns, 3 tool calls.
- Qwen: 5 turns, 3 tool calls.
- MiMo: 5 turns, 4 tool calls.
- GLM: 4 turns, 3 tool calls.

No provider hit a DOM submission failure, navigation abort, rate-limit recovery
button, duplicate send, or response timeout during this smoke. Provider code
was therefore unchanged.

A later exploratory A/B tested whether appending bounded verbatim excerpts from
already-visible failed-check output should become a new production feature.
DeepSeek baseline completed in 7 turns / 6 tool calls with 1 read after failure;
the context arm regressed to 8 turns / 7 tool calls with 2 reads after failure.
Both were correct, but the added excerpt supplied no new facts and increased
exploration. A MiMo baseline stalled and was discarded as an invalid sample.
The proposed verification-failure context was therefore withdrawn: no parser,
Agent integration, provider branch, version bump, or permanent probe was kept.

A canonical JSON tool contract fixture now snapshots the ordered `TOOL_SPECS`
fields and the exact rendered contract inserted into the web-model prompt.
Intentional protocol changes must update the reviewed fixture explicitly;
parser behavior tests continue to own required arguments and invalid
combinations. This guard adds no runtime code, prompt text, UI, or model call.

Validation: `python -B -m pytest -q` passed with 654 tests, 8 skips, and 31
subtests. `python -B -m unittest` passed with 654 tests and 8 skips. Full-tree
Ruff, `compileall`, and `git diff --check` passed. Pytest emitted one harmless
warning because the sandbox could not update `.pytest_cache`.

## 0.1.34 Bounded Edit Failure Context

Exact replacement writes remain unchanged. On failure, `edit_file()` may add a
bounded, line-numbered excerpt only when it finds a unique lexical anchor from
the submitted `old_string` in the original disk content. Missing anchors retain
the generic read-again guidance. Multiple exact matches report at most three
start lines. Output is capped at 1,600 characters and seven complete source
lines; overlong lines are omitted with an offset-read instruction.

Atomic multi-replacement failures identify the failed replacement and state
that nothing was written. All failure evidence comes from the original disk
content, never the partially updated in-memory value. Unit coverage includes
stale comments, absent anchors, bounded duplicate positions, omitted matches,
identifier boundaries, quoted literal semantics, long lines, total budget, CRLF
line numbers, late atomic failure, intermediate duplicate matches, unchanged
files, unchanged success output, and tool-result-like source text. A local
100,000-match `edit_file()` regression probe completed in approximately 0.017
seconds and left the file unchanged.

Anchor discovery is capped at 32 deduplicated candidates after a stable
longest-first sort. A local 1,000-candidate probe over approximately 475 KiB
completed in about 0.242 seconds and safely returned no context, compared with
the previously observed multi-second unbounded scan. The JSON tool contract now
states consistently that `content` is only for new-file creation; existing
files must use exact replacements even when JSON escaping is difficult.

Live stale-read A/B used production `edit_file()` for both arms and disabled
only the context renderer for baseline. DeepSeek improved from 6 turns / 5 tool
calls / 1 reread to 5 / 4 / 0. Qwen improved from 7 / 6 / 1 to 5 / 4 / 0.
MiMo improved from 6 / 5 / 1 to 5 / 4 / 0 on the completed comparison; one
separate MiMo context attempt hit a 300-second webpage timeout before a clean
single-arm rerun passed. GLM baseline passed at 6 / 6 / 1. GLM context preserved
correctness and removed the reread, but was variable: one run reached max turns
after producing a correct tested file, and a clean rerun finished at 7 / 5 / 0.
Across completed arms, the target edit was correct, the external comment was
preserved, the unrelated default was unchanged, and local tests passed.

Validation: `python -B -m unittest` passed with 630 tests; `python -B -m pytest
-q` passed with 638 tests and 31 subtests. Full-tree Ruff, `compileall`, manual
probe self-test, and `git diff --check` passed.

## 0.1.33 Read-before-edit Guard

This release adds a run-scoped guard for existing files: the Writer must
successfully `read_file` a file in the current agent run before a replacement
edit may change that existing file. Full `content` writes are only allowed for
new-file creation, so a paginated read cannot unlock whole-file replacement.
Files created or changed in the same run become known for follow-up replacement
edits. `grep`,
`find_references`, Project Map, and Symbol overview remain navigation hints, not
edit permission.

Targeted tests cover blind existing-file edits, failed reads, new-file creation,
same-run follow-up edits, `read_files`, `parallel(read_file)`, path
normalization, baseline capture, and rejection of whole-file `content` writes
after a paginated read. A fake-provider flow verifies the model can recover
after the guard blocks a blind replacement by reading the file and retrying.

Live Edge/CDP A/B across DeepSeek, Qwen, MiMo, and GLM ran three small project
tasks each with the guard disabled and enabled. The four web models generally
read files before editing, so the guard did not need to block any live edit. The
important live signal is that the guard did not add visible burden or regress
success on compliant providers: DeepSeek, Qwen, and MiMo passed all 6/6
baseline/guard arms; GLM passed 4/6 arms, with its failure unrelated to the
guard because no read-before-edit block fired. The GLM failure exposed smart
quotes in Python edit JSON. A diagnostic route-case rerun passed both arms after
an experimental snippet normalization, but that normalization was removed
before release because it could alter legitimate string content. A later full
GLM rerun was stopped when the site remained rate-limited, so neither run is
reported as a current-build full-matrix pass.

DeepSeek provider reliability also improved: when the page shows the visible
yellow rate-limit retry button with "消息发送过于频繁", Codey waits a short
cooldown, clicks the retry button, and continues waiting for the answer.
GLM applies the same bounded recovery when it shows "请求过于频繁，请稍后再试"
with a visible "重新回答" action. Both providers may retry repeatedly after
cooldowns, bounded by the original request timeout.

GLM smart-quote normalization first accepts already-valid JSON unchanged except
for compile-gated full Python `content`. Structural quote repair runs only after
the original JSON fails to parse, and its candidate must parse successfully
before use. It preserves `old_string`, `new_string`, summary text, and
replacement items exactly, including legitimate typographic punctuation.

The initial project prompt now gives one stable relative-path workspace rule
instead of exposing an absolute temporary project path. The entire Project
instructions section is omitted when neither `AGENTS.md` nor `CLAUDE.md` exists;
when present, those files are still loaded and bounded as before. Unit tests
capture both prompt forms.

A lightweight live guard smoke used one `similar-config-constant` task per web
provider after this prompt change. DeepSeek, Qwen, MiMo, and GLM each passed in
4 turns, changed only `limits.py`, and passed the two local checks. Tool calls
were 3 / 3 / 4 / 3 respectively. No guard block, navigation error, duplicate
submission, rate-limit recovery, or protocol repair occurred.

Validation: `python -B -m unittest` passed with 613 tests; `python -B -m pytest
-q` passed with 621 tests and 31 subtests. Full-tree Ruff, `compileall`,
manual `read_before_edit_ab.py --self-test`, and `git diff --check` passed.

## 0.1.32 Bounded Symbol Overview

This release adds a bounded task-aware Symbol overview as a small section of the
existing Project Map. It gives the Writer file and symbol navigation hints
before the first read, while keeping the old Project Map role for manifests,
docs, roots, candidate commands, and observed checks. It adds no UI, public
tool, persisted index, cache, embedding, LSP integration, or source bodies.

The live Edge/CDP A/B benchmark asks each provider to choose the first files it
would inspect for five Codey maintenance tasks. The final production
`render_project_map(..., task=...)` path improved first-file selection across
all four web providers:

| Arm | Expected-path hits | Top-1 hits |
|---|---:|---:|
| Initial listing only | 18 | 7 / 20 |
| Project Map | 23 | 8 / 20 |
| Project Map + Symbol overview | 40 | 20 / 20 |

Provider-level Symbol overview top-1 result: DeepSeek 5/5, Qwen 5/5, MiMo 5/5,
GLM 5/5. The manual benchmark now lives at
`tests/manual/project_map_symbol_ab.py` and tests the production Project Map
renderer directly.

Qwen reliability also improved: `new_chat()` now tolerates Qwen redirect
`net::ERR_ABORTED` before readiness checks, and `chat()` retries once when a
confirmed submission stalls without producing a response. Other navigation and
timeout failures remain visible.

Validation: `python -B -m unittest` passed with 590 tests; `python -B -m pytest
-q` passed with 598 tests and 31 subtests; changed-file Ruff, `compileall`,
manual Symbol overview self-test, and `git diff --check` passed.

## 0.1.31 Structured Execution Evidence

This release adds a bounded, in-memory evidence ledger for one TaskRunner run.
It records edit epochs, changed files, read ranges, complete and truncated
searches, truncated tool results, duplicate information calls, and successful
or failed checks after the latest edit. Verification Map, Review, receipts, and
successful ProjectFacts now consume the same current check facts. The ledger is
not persisted, adds no UI or public tool, and does not intervene in Writer
convergence.

Live Edge/CDP reference-aware edits passed on all four providers:

| Provider | Result |
|---|---|
| MiMo | passed in 6 turns |
| Qwen | passed in 7 turns |
| GLM | passed in 8 turns |
| DeepSeek | passed in 9 turns |
| MiMo Writer -> Qwen Reviewer | approved; independent unittest passed |

The live run exposed a GLM duplicate-submission false positive. GLM can append
different question nodes for tool results and protocol repair; duplicate
detection now compares the normalized full prompt body for this submission
instead of treating total question-node growth as duplication.

Validation: `582 passed`, `31 subtests passed`, Ruff, `compileall`, and
`git diff --check` all pass.

## Post-0.1.29 Four-Provider Capability Audit

A repeated live Edge/CDP audit evaluated the distinct capabilities introduced
from `0.1.25` through `0.1.29` instead of treating one read-only task as proof
for every release. DeepSeek, GLM, Qwen, and MiMo were exercised on bounded
navigation, reference-aware modification, interrupted-task recovery, and
Verification Map review. The detailed methodology and results are recorded in
`tests/manual/POST_0_1_29_CAPABILITY_AUDIT.md`.

The audit exposed and fixed three concrete reliability problems: literal grep
silently skipped source files larger than 512 KiB, GLM smart quotes could
corrupt Python source embedded in an edit command, and the manual benchmark
could fail while printing Unicode through a GBK Windows console. Search now has
an explicit 8 MiB per-file and 16 MiB total read budget, GLM Python content is
repaired only when the original fails compilation and the quote-normalized
candidate compiles, and manual benchmark output is UTF-8.

Current validation: `575 passed`, `31 subtests passed`, changed-file Ruff,
`compileall`, and `git diff --check`.

## 0.1.30 Outline Tool Withdrawal

The `outline_file` capability introduced briefly in 0.1.26 is now removed from
the public contract, Agent dispatch, hidden audit, runtime, parser, and tests.
Natural tasks consistently selected literal grep or lexical references followed
by offset reads. There is no compatibility alias. The supported navigation chain
is Project Map, grep or find references, then `read_file(offset=...)`.

Validation: `575 passed`, `31 subtests passed`, Ruff, `compileall`, and
`git diff --check` all pass.

Date: 2026-07-11
Environment: Windows / Edge or Chrome CDP reuse path / DeepSeek, MiMo, Qwen, GLM tabs open

## 0.1.29 Verification Map

This release adds bounded, evidence-labeled verification candidates to Review.
It does not claim affected tests, dependency impact, or coverage.

Behavior:

- changed tests are listed directly;
- Python and JS/TS tests can be candidates through strong filename relations;
- Python direct imports use `ast`; relative JS/TS imports use bounded lexical parsing;
- only a small set of declarations added by the diff may produce reference hints;
- successful checks are taken only from after the latest real edit;
- manifest and previously successful project checks are separate broader candidates;
- one bounded traversal scans test files instead of rescanning per symbol;
- excluded directories, symlinks, non-UTF-8/binary, secret-like, and oversized
  files are skipped;
- scan, candidate, result, changed-file, check, and command budgets are explicit;
- attempted test content is capped at 16MB in addition to the 512KB per-file
  limit; non-UTF-8 files consume budget before decoding and cannot bypass it;
- a truncated diff or scan produces an explicit incomplete-discovery warning;
- no candidates renders `(none found)` without suggesting that testing is unnecessary;
- Reviewer is told that candidates are not coverage proof and candidate paths do
  not relax the changed-file-only finding contract;
- construction failure degrades to normal Review with an empty map;
- a later edit or failed verification run clears prior green checks in both
  memory and the durable checkpoint, keeping Review, recovery receipts, and
  successful ProjectFacts semantics aligned.

Live Edge/CDP verification used DeepSeek as Writer and GLM, Qwen, and MiMo as
Reviewers. The map identified `test_app.py` through naming evidence and reported
only the post-edit `python -m unittest`. Qwen initially misread an unchanged
candidate as a missing file; the map now states that candidates are existing
readable local files and absence from Changed files means unchanged. After that
root fix, all three reviewers approved the independently verified change without
a noisy follow-up, and checkpoint recovery remained successful.

Regression result: `573 passed, 33 subtests passed`; changed-file Ruff,
`compileall`, and `git diff --check` passed.

## 0.1.28 Durable Execution Checkpoint

This release adds a bounded, atomic, session-scoped checkpoint for unfinished
project execution. It stores local execution facts only: relative changed
paths with post-edit hashes, successful checks after the latest edit, a small
last-action record, status, and stop reason. It stores no source, diff, tool
output, prompt, task plan, or model-authored remaining work.

Covered behavior:

- successful edits invalidate all earlier checks even if a path/hash cannot be
  recorded; runtime-accepted relative or in-project absolute paths are
  canonicalized under the project root before hashing;
- successful runs are recorded, while failed runs remain only the last action;
- recovery reconciles hashes and invalidates checks after external changes;
- stopped, errored, max-turn, and no-progress tasks retain an interrupted checkpoint;
- only the same session/project with an explicit continuation, or the same task
  after provider-context loss, receives the recovered facts;
- unrelated new tasks do not inherit an old checkpoint;
- recovered changes still enter final Diff/Review even if the resumed Writer
  performs no additional edit;
- a failed verified ProjectFacts write does not delete the checkpoint early;
- normal completion removes the active checkpoint;
- corrupt, oversized, or incompatible checkpoint files are ignored safely.

Live Edge/CDP verification intentionally stopped DeepSeek at `max_turns=1`
immediately after creating `app.py`, closed the provider, opened a fresh
DeepSeek conversation with the recovered checkpoint, read the real files, ran
`python -m unittest`, completed successfully, passed an independent local
check, received GLM approval, and deleted the completed checkpoint.

Regression result: `555 passed, 33 subtests passed` with pytest; changed-file
Ruff, `compileall`, and `git diff --check` also passed.

## 0.1.27 Find References Tool

This release adds `find_references`, a bounded lexical reference-hints tool for
inspecting likely impact sites before changing a symbol. It does not add UI,
cache, indexing, LSP, semantic resolution, persistence, or a reviewer tool.

Behavior:

- `find_references` is exposed in the JSON tool contract and runs as local
  runtime tool `references`.
- The tool accepts only simple symbols such as `createRouter`,
  `SessionStore`, `login_user`, or `$state`; complex expressions should use
  `grep`.
- Results are explicitly labeled as lexical hints only, not semantic resolution
  or a complete call graph.
- Output is stable, path/line based, and capped at 80 matches with precise
  truncation metadata.
- The scanner skips dependency/build/cache directories, large files, unreadable
  files, non-UTF-8 files, and symlinked paths.
- Direct dependency/build/cache start directories are skipped case-insensitively
  instead of being scanned as a special case.
- A selected project root named like an excluded directory, such as `build`,
  `dist`, or `target`, still scans normally.
- Direct symlink start paths are rejected before resolving, so a project-local
  link cannot silently redirect the scan to its target.
- `find_references`, normal `grep`, and hidden project-audit search/reference
  scans share the same streaming bounded scanner; none of them collects and
  sorts every candidate file before starting work.
- When the shared scanner reaches a file, directory, or per-directory entry
  budget, the tool result explicitly says omitted files may contain more
  matches.
- `find_references` is not `parallel_safe`; `parallel` still accepts only
  `list_dir`, `read_file`, and `grep`.
- Hidden project-audit advisors may use `find_references`, but only through the
  same sensitive path, symlink, binary, excluded-directory, and size checks used
  by audit reads.
- Reviewer still receives diff/log/brief/map context only; no reviewer-side tool
  access was added.

Verification:

| Flow | Result |
|---|---|
| Runtime / protocol / agent / consensus / live-smoke focused suite | 160 passed |
| Full pytest suite | 542 passed |
| Syntax compile | `python -m compileall -q codey tests tools` passed |
| Diff whitespace check | `git diff --check` passed with no output |
| DeepSeek live `find_references` smoke | passed in 8 turns |
| GLM live `find_references` smoke | passed in 7 turns; unique indentation recovery exercised |
| Qwen live `find_references` smoke | passed in 7 turns |
| MiMo live `find_references` smoke | passed in 8 turns; edit path protocol correction exercised |

## 0.1.26 Outline File Tool (withdrawn)

`outline_file` was removed after the natural-use audit. Controlled prompts could
make providers use it, but normal tasks consistently preferred literal `grep`
followed by offset `read_file`. Removing the tool also removes its public/runtime
contract, hidden-audit branch, Python/JS/TS outline parser, and maintenance tests.
There is no compatibility alias. The supported navigation chain is now Project
Map, literal grep or lexical references, then offset source reads.

Verification:

| Flow | Result |
|---|---|
| Tool runtime / protocol / agent / consensus focused suite | 113 passed |
| Full unittest suite | 510 passed |
| Full pytest suite | 510 passed |
| Syntax compile | `python -m compileall -q codey tests tools` passed |
| Diff whitespace check | `git diff --check` passed with no output |

## 0.1.25 Hidden Project Map

This release adds a deterministic, bounded Project Map as a hidden first-turn
orientation layer. It does not add UI, storage, indexing, RAG, or another model
call.

Behavior:

- Before project tasks, Codey builds one bounded local Project Map and passes it
  to the Writer.
- The same Project Map is shared with hidden consensus/project-audit advisors so
  advisors start from the same safe local structure.
- Review receives a refreshed Project Map after Writer changes are collected, so
  newly added files such as tests or manifests are visible to the Reviewer.
- Review path rules remain strict: Project Map is only context for coverage and
  integration judgment; findings still must point to changed files.
- The map includes only relative-path facts: source/test roots, manifests, docs,
  key directories/files, observed successful checks, and candidate commands.
- Candidate commands are explicitly marked as candidates, and nested manifests
  carry their working directory, for example `web/: npm test`.
- The scanner does not read source contents, does not follow symlinks, skips
  dotfiles, secret-like files, env/key/certificate files, lock files,
  dependency/build/cache directories, and uses a strict directory-entry budget.

Verification:

| Flow | Result |
|---|---|
| Project Map unit tests | 8 passed |
| Project Map / Writer / consensus / Review / server focused suite | 160 passed |
| Full unittest suite | 501 passed |
| Full pytest suite | 501 passed |
| Syntax compile | `python -m compileall -q codey tests tools` passed |
| Diff whitespace check | `git diff --check` passed with CRLF warnings only |

## 0.1.24 Hidden ChangeBrief and Plan-Aware Review

This release borrows the smallest useful part of lightweight spec workflows:
existing exploration output is converted into a hidden, bounded task brief.
No visible spec UI, project artifact, storage migration, command syntax, or
confirmation gate was added.

Behavior:

- Empty-project hidden planning now becomes a private `ChangeBrief` before the
  Writer starts.
- Existing-project read-only audit reports now become the same private
  `ChangeBrief` format.
- The Writer receives the `ChangeBrief` instead of loose advisory prose.
- The Reviewer receives the same `ChangeBrief` and checks intent satisfaction,
  acceptance checks, non-goals, and risks in addition to diff correctness.
- Simple direct write tasks that do not have hidden planning or audit context do
  not receive a `ChangeBrief`.
- Successful project facts now include recent verified changes, but only after
  a task finishes with real file changes and a passing local check.
- Successful-change facts store only task excerpt, changed files, successful
  check commands, and the task receipt. Model-authored behavior summaries are
  not persisted.
- Existing `facts.json` command-only state remains valid; a missing
  `successful_changes` field is treated as empty.

Verification:

| Flow | Result |
|---|---|
| ChangeBrief/review/project-facts/server focused suite | 97 passed |
| MoA snake flow script unit tests | 6 passed |
| Full unittest suite | 491 passed |
| UI browser E2E | Included in full unittest; hidden project consensus updated for `ChangeBrief` |
| Syntax compile | `python -m compileall -q codey tests tools` passed |
| Diff whitespace check | `git diff --check` passed with CRLF warnings only |

Live MoA snake flow:

```powershell
python -B tests\moa_snake_flow.py --project E:\snake --reset --json
```

Result:

- Passed on 2026-07-10 with DeepSeek as Writer and GLM, MiMo, and Qwen as
  advisors/reviewers.
- The flow covered New Chat MoA discussion, empty-project implementation,
  independent local verification, explicit diff review across GLM/MiMo/Qwen,
  explicit read-only project audit across GLM/MiMo/Qwen, Writer follow-up, and
  final independent verification.
- The generated `E:\snake` project passed `python -B -m unittest` with 12 tests.
- Smoke artifacts are written under the target project at
  `E:\snake\.codey\smoke\moa-snake-flow`, not under the Codey repository.
- The second full run reported no provider errors and no single provider send
  above the 120-second slow threshold. The first probe exposed one transient
  Qwen textarea click timeout during a borrowed-advisor follow-up; it did not
  reproduce in the clean rerun, so no provider-specific workaround was added.

## 0.1.23 Browser Launch Robustness and Explicit Truncation Guidance

This release keeps the UI unchanged while making browser startup failures
actionable and local truncation visible to the model.

Behavior:

- Browser auto-launch honors `CODEY_BROWSER_PATH` when it points to an existing
  executable.
- If no explicit path is configured, Codey still prefers Edge default install
  paths and falls back to system or per-user Chrome install paths.
- Auto-launched Edge and Chrome use separate profiles:
  `~/.codey/edge-profile` and `~/.codey/chrome-profile`.
- Missing browser startup now reports a clear Edge/Chrome/CODEY_BROWSER_PATH
  error.
- If `webview.start()` fails, Codey prints the local URL, explains that the
  native window could not open, and keeps the HTTP server available until
  Ctrl+C.
- `ToolResult` now carries `truncated`.
- Model-facing tool results mark truncated output with `truncated=true` and
  explicitly say omitted content may still contain relevant errors or code.
- Approved Shell continuation also tells the writer when Shell output was
  truncated.
- Review prompts warn when the diff was truncated and instruct the reviewer not
  to assume omitted hunks are clean.
- No read/run/diff size limits were increased, no full output replay was added,
  and no UI was added.

Verification:

| Flow | Result |
|---|---|
| Browser/protocol/review/server focused suite | 130 passed |
| Full unittest suite | 478 passed |
| Syntax compile | `python -m compileall -q codey tests tools` passed |
| Diff whitespace check | `git diff --check` passed with CRLF warnings only |
| Browser fallback | `CODEY_BROWSER_PATH`; Edge first; system/per-user Chrome fallback; separate profiles |
| WebView fallback | HTTP server remains available with manual URL after native-window failure |
| Truncation guidance | `truncated=true` tool results and diff truncation review note |

## 0.1.22 Durable Conversation Handoff

This release connects durable visible chat history to the existing compact
conversation handoff. The goal is to reduce model "amnesia" after a Codey
restart or model switch without replaying the full transcript or adding UI.

Behavior:

- `UiStateStore` can extract a bounded visible excerpt for one saved session.
- The excerpt includes only recent user messages, assistant replies, Review
  receipts, Done receipts, and compact Changes receipts.
- Tool events, turn markers, Shell output, teaching cards, and other local
  execution noise are skipped.
- The current user request is skipped so continuation prompts do not duplicate
  the active message.
- Fresh web-model chats after restart, model switch, or provider-session loss
  merge the compact factual snapshot with the bounded visible excerpt.
- The selected model receives the recovered handoff and can continue the
  conversation naturally.
- Hidden MoA advisors still receive only the compact factual snapshot; the
  recovered visible chat excerpt is not automatically spread to other web
  models.
- If no visible excerpt exists, normal MoA chat keeps using the previous compact
  context path and does not create an owner-only recovered prompt.
- No full transcript replay, export button, training pipeline, or new UI mode
  was added.

Verification:

| Flow | Result |
|---|---|
| Focused recovered-handoff suite | 72 passed |
| Full unittest suite | 469 passed |
| Syntax compile | `python -m compileall -q codey tests tools` passed |
| Diff whitespace check | `git diff --check` passed with CRLF warnings only |
| Recovered owner prompt | Selected model gets compact snapshot plus bounded visible excerpt |
| Hidden advisor context | MoA advisors keep compact factual snapshot only |
| Current request duplication | Current user request is skipped from recovered visible excerpt |
| Export/training UI | None |

## 0.1.21 Durable Chat State and Quiet Controls

This release makes visible chat state durable while keeping the UI small and
local-only. It also keeps the quiet per-message copy control and the shared
Send/Stop action slot.

Behavior:

- User messages, assistant replies, Review receipts, and Done receipts expose a
  per-message copy button.
- Copy uses the browser Clipboard API with an `execCommand("copy")` fallback.
- The copy control stays subdued by default and becomes clear on hover or
  keyboard focus.
- Send and Stop now share one composer action slot.
- Idle state shows `Enter` plus the send arrow.
- Running state shows `Stop` plus the square stop icon.
- `updateSend()` is the single path that switches send, stop, and hint state.
- The native WebView runs with `private_mode=False` and persistent storage at
  `~/.codey/webview`.
- Chat sessions, titles, messages, terminal receipts, active chat, and sidebar
  projects are stored as one bounded backend snapshot at
  `~/.codey/ui-state.json`.
- Browser `localStorage` remains a fast cache; `/api/ui_state` is the durable
  recovery source.
- UI state recovery runs before opening the SSE event stream, so reconnect or
  task events are not dropped while restored sessions are still loading.
- UI state writes use `(updated_at, revision)` ordering so same-millisecond
  async saves cannot overwrite newer state.
- The backend snapshot keeps only the UI schema fields Codey renders, with
  bounded sessions, projects, messages, terminal runs, and string lengths.
- No export button, training pipeline, or chat database workflow was added.

Verification:

| Flow | Result |
|---|---|
| Focused UI-state suite | 43 passed |
| Full unittest suite | 464 passed |
| Syntax compile | `python -m compileall -q codey tests tools` passed |
| Diff whitespace check | `git diff --check` passed with CRLF warnings only |
| WebView persistence contract | `private_mode=False`; `storage_path=~/.codey/webview` |
| Backend UI snapshot | `~/.codey/ui-state.json` |
| Export/training UI | None |

## 0.1.19 MiMo Answer-State Completion

This release narrows MiMo's state split: send-button recognition can still use
the MiMo paper-plane SVG as a provider-specific final guard before clicking,
but answer completion no longer depends on the send icon returning.

Behavior:

- `_generation_complete()` now locates the newest answer and evaluates answer
  DOM state instead of composer icon state.
- Cleaned final text must exist after removing MiMo thinking/deep-thinking
  blocks.
- `data-is-typing="true"` rejects completion.
- A copy button near the newest answer marks the answer complete.
- If no copy button is available yet, completion requires that MiMo is not in a
  generating/stop state.
- SVG matching remains only in the send-button path to avoid upload/stop
  misclicks.

Verification:

| Flow | Result |
|---|---|
| MiMo unit suite | 29 passed |
| Focused provider/protocol/server suite | 211 passed |
| Full unittest suite | 452 passed |
| Syntax compile | `python -m compileall -q codey tests tools` passed |
| Diff whitespace check | `git diff --check` passed with CRLF warnings only |
| New UI modes or controls | None |

## 0.1.18 Provider Reliability and Factual Receipts

This release keeps the visible UI unchanged and tightens the provider and
review boundaries underneath it.

Changes:

- MiMo accepts only its explicit paper-plane send button and rejects nearby
  upload controls.
- MiMo treats the page as busy while the stop/generating state is active and
  refuses to submit another message during that state.
- MiMo removes thinking/deep-thinking blocks before reading the final answer
  and does not fall back to visible answer text while generation is active.
- Qwen uses a strict local `button.send-button` fallback before teaching, and
  hidden Review runs suppress manual teaching and ProfileDoctor assistance.
- GLM can detect a replaced answer when the response count does not increase,
  and GLM-only smart-quote normalization also covers review JSON.
- The shared JSON protocol rejects nested tool calls hidden inside
  `done(summary)` and directs models to call the tool directly.
- Legacy write-style tool names are no longer accepted; models must use
  `edit(content=...)` for creating or replacing a file.
- Review repair follow-up tracks whether the repair turn actually ran checks,
  so failed checks or unfinished repair turns no longer inherit a previous
  `checks passed` receipt.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 450 passed |
| Focused provider/protocol/server suite | 209 passed |
| Agent and server regression suite | 116 passed |
| Syntax compile | `python -m compileall -q codey tests tools` passed |
| Diff whitespace check | `git diff --check` passed with CRLF warnings only |
| MiMo unit contract | 27 tests passed; send button, upload rejection, stop-state rejection, thinking cleanup, and generation gating covered |
| Qwen unit contract | Strict local send fallback covered before teaching |
| GLM unit contract | Smart-quote review JSON and replaced-answer detection covered |
| Protocol contract | Nested tool calls inside `done(summary)` rejected; write-style tools rejected |
| Review receipt regression | Failed repair checks and no-progress repair turns do not inherit `checks passed` |
| Real MiMo single-send smoke | Final answer returned; no upload popover and no stopped-response state |
| Real DeepSeek + MiMo project audit smoke | MiMo returned a valid hidden audit report |
| Real DeepSeek write + MiMo review smoke | Review completed with `ok: true` and `approved` |
| New UI modes or controls | None |

## 0.1.17 Hidden MoA Layer

MoA is a hidden consultation layer, not a UI mode. When already-open
provider pages are available, `New Chat` asks the selected model for a private
draft, lets up to two other models critique or supplement it independently,
then asks the selected model to produce one final answer. Empty or
placeholder-only projects use the same owner-first pattern for one hidden
advisory plan before the Writer starts. Existing projects keep bounded
read-only advisor audits before the selected model acts; private audit reports
are passed to the selected Writer as advisory input, and the Writer still
verifies against real files.

Boundaries:

- Chat advisors cannot use tools, edit files, run commands, browse, or see the full project.
- Chat and empty-project advisors review the selected model's private draft; they do not see each other's notes.
- Project audit advisors can only list, grep, and read selected project files.
- Dotfiles, env files, secret-like paths, excluded dependency/build directories, key/certificate files, lock files, binaries, symlinks, and oversized files are not shared with hidden project audit advisors.
- Project audit advisors cannot edit, run commands, request shell approval, or access paths outside the project.
- While a Writer tab is active, hidden advisors are borrowed from already-open sibling tabs instead of opening another CDP connection.
- Each project audit advisor has a bounded total time budget; unfinished advisors produce no private report.
- If no advisor is available, the task falls back to the normal single-model path without generating a private draft.
- If draft-first advisors fail, the selected model's draft is used as the quiet fallback.
- If final synthesis fails after a private draft, Codey does not resend the original prompt.
- New Chat emits one ordinary assistant reply.
- Empty projects can receive one hidden plan before the selected Writer starts.
- Existing project audits are private reports; the selected Writer still verifies and decides.
- Project tasks that finish with `changed=False` can still refine the final read-only answer.
- Review remains a separate post-change acceptance layer over the final Diff.
- The tool protocol tells the model not to claim command, test, build, lint, or shell results unless they came from a local `run` or `shell` tool result.
- The web UI adds no buttons, modes, model-vote display, or group chat surface.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 424 passed |
| Consensus unit contract | Automatic advisor selection, owner draft, independent critiques, bounded prompts, failures degrade |
| Owner draft prompt with long handoff | Current user request remains a separate field and is not clipped away by handoff context |
| New Chat follow-up consensus context | Handoff is passed once as context; owner prompt stays empty to avoid duplicate handoff text |
| Server New Chat consensus | Agent loop not called; one `reply` emitted |
| New Chat draft-first consensus | Selected model draft is sent before advisors; degraded draft result forgets the provider session |
| New Chat synthesis failure | Original prompt is not resent after an unexpected hidden synthesis failure |
| Project audit advisor tools | Read-only file inspection works; attempted writes are rejected and files remain unchanged |
| Project audit secret boundary | `.env`, `prod.env`, credential files, excluded directories, symlinks, and secret search hits are not sent to hidden advisors |
| Project audit unfinished advisor | No `done(summary)` means no report is passed to the Writer |
| Sibling-tab advisor connection | Hidden consensus and project audit borrow already-open tabs from the active Writer context |
| GLM split markdown response | One assistant answer split across multiple `.markdown-body` nodes is read as one complete answer wrapper |
| GLM smart-quote tool JSON | GLM-only normalization repairs smart double quotes around tool JSON without changing the shared JSON codec |
| Qwen review path contract | Live Qwen review probe returned `game.js`, copied from Changed files, instead of inventing a new test filename |
| Existing project audit | Private reports are injected into the selected Writer task |
| Existing project audit failure | Writer continues without the private reports |
| Empty project plan | Draft-first hidden plan is injected before Writer starts in a fresh chat |
| Project read-only consensus | Final answer can be refined after a no-change project task; no Review |
| Project read-only synthesis failure | Writer answer is kept and the provider session is forgotten |
| Project write task | Writer edits; Review still runs after Diff |
| Real Edge UI E2E | All 21 checks passed, including hidden consensus and existing restore/reconnect paths |
| Live Edge MoA project review | Passed with DeepSeek Writer plus MiMo and Qwen hidden audit advisors; two reports collected, no `.env` marker leaked, no files changed |
| Live Edge snake stress loop | Root cause traced to GLM reading only the last `.markdown-body` fragment of a split answer plus occasional smart quotes; wrapper-based GLM response reading and GLM-scoped smart-quote cleanup were added |
| New UI modes or controls | None |

## 0.1.16 Project and Plain Conversations (2026-07-06)

`New Chat` remains a normal model conversation with no project path and never
enters the local Agent tool loop. A project conversation now supports both
read-only discussion and coding without a mode switch: the selected model may
inspect the project and answer directly, but edits only when requested. The
second model is used for Review only when the current Agent run actually wrote
a file.

The project answer and optional change receipt are two views of the same
`task_done` event. This keeps reconnect recovery atomic and deduplicated while
hiding the unhelpful `No files changed` receipt for discussion-only turns.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 388 passed |
| Global New Chat route | Direct Provider reply; Agent tool loop not called |
| Project read-only discussion | Multiline answer preserved; no writes and no Review |
| Project edit task | Final answer shown before tested change receipt and Diff |
| Real Edge UI E2E | All 19 checks passed, including plain chat, project discussion, and reconnect recovery |
| Live DeepSeek project discussion | Passed in 1 turn; no files created or changed |
| New UI modes or controls | None |

## 0.1.15 GLM Provider

GLM is registered as a fourth provider and reuses the existing browser,
one-shot submission, cancellation, recovery, diagnostics, and task-context
boundaries. Its local profile anchors the composer, accepts the send control
only after non-whitespace text enables it, and selects the final Markdown
answer while excluding the neighboring thinking panel. A GLM-only formatting
note puts JSON in a code block with ASCII quotes; normal chat remains normal.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 384 passed |
| Provider registry, profile, browser wrapper, CLI, and smoke choices | GLM included and cross-checked |
| Blank or whitespace-only composer state | Send remained unavailable |
| Blank `GlmWebProvider.send()` input | Rejected before formatting or page access |
| Non-empty composer state | Send became available and was verified before click |
| Thinking and final answer DOM | Final answer selected independently |
| Discovery, Doctor, teaching, or cached response inside/around thinking DOM | Rejected before text is read |
| One-shot uncertain submission | No second click; delayed answer still observed |
| Duplicate question guard | Extra user question is reported rather than silently accepted |
| Full 4K tool protocol on live GLM page | Returned valid ASCII JSON; one question after completion |
| Live GLM edit task, first run | Passed in 4 turns; independent unittest passed |
| Live GLM edit task in four-provider matrix | Passed in 4 turns; independent unittest passed |
| Live GLM review of a DeepSeek edit | Approved; independent unittest passed |
| Live GLM edit after final-answer guard | Passed in 5 turns; independent unittest passed |
| Real Edge UI E2E | All 16 checks passed, including GLM picker selection |
| Existing DeepSeek and MiMo matrix tasks | Passed in 4 / 4 turns |
| Qwen startup root cause | Composer appeared before `/api/v2/models/` completed; click reached the React handler but emitted no chat request |
| Qwen startup repair | Waits for successful model bootstrap; necessary draft settling and A/B choice handling remain |
| Fresh Qwen edit reruns | Passed in 4 / 5 turns; both independent unittest checks passed |
| New UI concepts | None; one provider row added |

## 0.1.14 Protocol Efficiency and Safety

The JSON tool protocol now derives its public names, aliases, examples,
read-only properties, and result labels from one compact contract. Unsafe
`parallel` batches are rejected as a whole before execution. `read_file`
returns large UTF-8 files in complete-line pages with bounded line and file
content character counts, while preserving the exact output of small and empty files.
Structured `replacements` apply up to eight changes to one file after every
search is validated in memory; a failed later search leaves the file intact.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 363 passed |
| Tool contract examples and aliases | Generated and parsed from one immutable specification |
| Unsafe `parallel` with a valid read before an edit | Entire batch rejected; no tool event or file change |
| `parallel` wrappers, commands, writes, nesting, and over-limit batches | Rejected before execution |
| `read_file` pagination | Complete lines, explicit next offset, 300-line default, 600-line maximum, 16,000-character file-content budget |
| Empty and small files | Previous output preserved exactly |
| Overlong single line | Bounded head/tail preview marked unsafe for `old_string` |
| Atomic replacements | Later validation failure leaves original bytes unchanged; eight-item limit enforced in codec and runtime |
| Real Edge UI E2E | All 16 checks passed |
| Live DeepSeek edit task | Passed in 4 turns; unittest passed |
| Live Qwen edit task | Passed in 4 turns; `read_files` used; unittest passed |
| Live MiMo edit task | Passed in 4 turns; unittest passed |
| New UI controls or cards | None |

## 0.1.13 Runtime Ownership and Provider Context

Git and snapshot change collection now share `changes.py`, while the HTTP
server retains only transport and request orchestration. Edge profile, CDP
state, learned controls, continuity data, and recovery snapshots use one local
state root. UI, CLI, and smoke tasks explicitly own provider-local context and
clear it after every exit path, including connection and CDP close failures.
Qwen keeps one real trailing keyboard input so its controlled composer commits
the same text that Codey verifies and submits.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 344 passed |
| Git and snapshot change ownership | Unified in `changes.py`; server transport unchanged |
| Local runtime paths | Edge profile, CDP state, controls, facts, conversations, and snapshots share one root |
| Provider task-local context | Cleared after success, cancellation, task failure, connection failure, and CDP close failure |
| CLI and smoke close-failure regression tests | All four entry points passed |
| Removed runtime residue | Unused `State.events` queue removed |
| Real Edge UI E2E | All 16 checks passed |
| Live DeepSeek edit task | Passed in 5 turns; unittest passed |
| Live Qwen edit task | Passed in 9 turns; all submissions confirmed and unittest passed |
| Live MiMo edit task | Passed in 4 turns; unittest passed |
| New UI controls or cards | None |

## 0.1.12 Run Reconciliation and One-Shot Submission

The backend now reserves a unique run ID before browser work is queued and
keeps one bounded in-memory snapshot of the active run, pending approval or
teaching request, and last terminal event. After an SSE reconnect, the UI
reconciles against that snapshot, restores the existing controls, and dedupes
the terminal receipt by run ID. Short interruptions stay silent; only a
connection that remains unavailable for five seconds reuses the existing
status line for `Reconnecting…`.

DeepSeek, Qwen, and MiMo now share one submission boundary. Each provider
chooses click or Enter before the remote action, marks the attempt first, and
performs no second action after an exception or unconfirmed result. An
uncertain attempt continues through the complete original response window and
is accepted if the answer appears later. Qwen input uses its real keyboard
path because live testing showed that direct DOM filling changed the visible
textarea without updating the website's send state.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 331 passed |
| Atomic run reservation and snapshot isolation | Passed |
| Approval and teaching recovery after reconnect | Passed |
| Chat clear revokes the matching saved terminal receipt | Passed |
| Terminal receipt deduplication by run ID | Passed |
| Delayed, quiet reconnect status | Passed |
| Delayed answers after uncertain submission on all three providers | Passed |
| Shell result from HTTP while SSE is closed | Passed and deduplicated |
| Shell result snapshot after both HTTP and SSE are lost | Restored once; approval card removed |
| Disconnected Allow followed by completed continuation | Executed appears before Done |
| Stale busy snapshot returned after newer task_done SSE | Buffered event replay leaves UI done |
| Real Edge UI E2E | All 16 checks passed, including five reload/reconcile flows |
| Live DeepSeek edit task | Passed in 5 turns; unittest passed |
| Live Qwen edit task | Passed in 4 turns; unittest passed |
| Live MiMo edit task | Passed in 4 turns; unittest passed |
| New UI controls or cards | None |

## 0.1.11 Responsive Stop and Bounded Output

One task-local cancellation signal now covers provider polling, browser and
control recovery waits, ProfileDoctor, review, and controlled `run` processes.
Stopping does not click a provider website's stop-generation control. It
discards the provider session, reports `task_done: stopped`, and makes the next
task open a fresh conversation. Controlled subprocesses are terminated with
their process trees. Long `run` and approved Shell output share one pure
head-and-tail clipping function and do not retain a hidden full log.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 310 passed |
| Cancellation and real Windows parent/child process-tree tests | Passed |
| Provider, ProfileDoctor, Review, and TaskRunner cancellation tests | Passed |
| Head-and-tail output tests for `run` and approved Shell | Passed |
| Real Edge UI E2E | All 10 checks passed, including responsive Stop |
| Live DeepSeek cancellation | Cancelled; fresh conversation opened |
| Live Qwen cancellation | Cancelled in under 1 ms measured latency; fresh conversation opened |
| Live MiMo cancellation | Cancelled in under 1 ms measured latency; fresh conversation opened |
| UI surface | Unchanged |

## 0.1.10 ProfileDoctor

ProfileDoctor runs only after deterministic local recovery cannot choose a
provider-page candidate safely. It exposes at most eight candidates, removes
conversation and answer text, reduces labels and DOM identifiers to a fixed
semantic vocabulary, and replaces exact geometry with coarse buckets. One
already-open sibling provider may return only a candidate ID or `null`. The
request is one-shot, assistance is disabled during the call to prevent
recursion, and the existing state validation remains the only path that can
persist a learned control. A failed or invalid decision falls through to the
existing human teaching flow.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 294 passed |
| One-call, no-recursion, strict decision-schema tests | Passed |
| Sanitization and bounded-candidate tests | Passed |
| Malicious tag / role / type values | Reduced to fixed `other` enum values |
| Deterministic → Doctor → human ordering tests | Passed |
| Validation-before-save and storage-failure tests | Passed |
| Same-CDP sibling-tab borrowing tests | Passed |
| Real Edge UI E2E | All 9 checks passed; UI unchanged |
| Forced DeepSeek / Qwen / MiMo send recovery | Doctor selected one candidate; submission verified before save |
| Provider-added response status text | Parsed deterministically without a second model call |

## 0.1.9 Bounded Provider Recovery

The three provider drivers now read their normal selectors from one validated,
versioned local profile. If those selectors stop matching, a bounded discovery
layer scores only composer controls and DOM regions changed after submission.
Actionable learned selectors must resolve to one visible control, and Codey
persists them only after observing successful input, submission, or answer
reading. Two failed validations remove a learned record. Optional learning
storage is atomic and cannot fail an otherwise successful provider operation.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 277 passed |
| Provider-focused tests | 51 passed |
| Real Edge UI E2E | All 9 checks passed; UI unchanged |
| Normal DeepSeek / Qwen / MiMo tasks | Passed in 5 / 4 / 4 turns |
| Forced DeepSeek core-selector failure | Recovered; input, send, and answer records verified |
| Forced Qwen core-selector failure | Returned `RECOVERY_OK`; all three records verified |
| Forced MiMo core-selector failure | Returned `RECOVERY_OK`; all three records verified |
| Optional storage failure | Did not interrupt the successful provider operation |
| Syntax and patch checks | Passed |

The recovery scope remains deliberately small. It does not bypass login or
CAPTCHA challenges, click low-confidence controls, retain page DOM, or add any
new interface concept. Human one-click teaching remains the final fallback.

## 0.1.8 Hidden Local Continuity

Version 0.1.8 added three bounded, invisible continuity mechanisms:

- Project Facts records only successful controlled `run` commands and injects
  them into later tasks for the same project.
- Conversation persistence stores one compact factual snapshot for each recent
  chat, so a restarted Codey process opens a fresh provider chat with a silent
  handoff instead of losing the task state.
- Durable Snapshot atomically stores the non-Git recovery baseline before a
  project write, retains expected after-content hashes for conflict detection,
  and deletes the record after a successful restore.

The three mechanisms share only a small atomic JSON writer. They use separate
files and retention rules, and none adds a UI control, banner, or status message.
Git projects continue to use Git and do not persist a recovery snapshot.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 260 passed |
| Abrupt child-process exit | Facts, conversation, diff, and restore survived |
| Snapshot save failure | Project write was blocked |
| Manual edit after Codey write | Restore conflict preserved user content |
| Missing post-write hash after an interrupted write | Restore refused to overwrite current content |
| Chat deletion racing a late save | Deleted context stayed deleted |
| Non-Git project initialized as Git | Old tracker persistence was disabled; stale references could not recreate recovery |
| Real Edge UI E2E | Passed; UI unchanged, diff and restore passed |
| DeepSeek / Qwen / MiMo edit matrix | Passed in 5 / 4 / 4 turns |
| Independent provider verification | All three unittest fixtures passed |

The live provider run also emitted Node's existing `DEP0169` warning from the
browser automation runtime. It did not affect any provider result and is not
introduced by the local continuity code.

## 0.1.7 Maintainability Refactor

The agent now produces one structured `RunEvent` stream. CLI logs, review
context, SSE updates, and the UI are projections of that stream; the UI no
longer parses human log strings. Local tools return structured `ToolOutcome`
values, so check success and exit codes are not inferred from display text.
Task orchestration moved from the HTTP handler module into `task_runner.py`.

No legacy `agent.tool_*` wrappers were retained solely for tests. Tests now
exercise `tool_runtime.py` directly.

Follow-up regressions cover successful empty-file reads, no-op writes and
edits, and the absence of the legacy string `log` SSE path. A successful tool
call now counts as progress only when `ToolOutcome.changed` is true.

Verification after the refactor:

| Flow | Result |
|---|---|
| Full unittest suite | 224 passed |
| Real Edge UI E2E | Pass, including diff, restore, and shell denial |
| DeepSeek / Qwen / MiMo edit matrix | Pass, 5 / 4 / 4 turns |
| DeepSeek / Qwen / MiMo create matrix | Pass, 4 / 4 / 3 turns |
| Three writer/reviewer pairs | All approved |
| DeepSeek / Qwen / MiMo self-bootstrap | Pass, 5 / 5 / 6 turns |
| Same-model chat continuation | Marker preserved |
| DeepSeek to Qwen handoff | Marker and decision preserved |

Every live project smoke used a temporary directory and independent
post-agent verification. Every bootstrap smoke repaired a temporary Codey
copy and then passed all 219 tests.

## 2026-06-30 End-to-End Coverage

The UI now has a repeatable browser E2E test that launches real headless Edge
against the real local HTTP/SSE server. A deterministic provider drives the
agent so the test can assert the complete product flow without depending on
model variability:

- add a project through the UI
- select a provider
- submit a task and receive SSE updates
- write a file and run its test
- display review and task receipt status
- open and expand the diff drawer
- restore the snapshot and verify the file is removed
- deny a shell approval request and verify no command is executed
- capture screenshots for the completed and restored states

```text
python -B tools/ui_e2e.py --artifacts .e2e-artifacts --json
PASS
```

The live smoke runner now supports a three-provider matrix and independently
verifies the temporary project after the agent finishes. A model returning
`done` is no longer enough to pass: Codey separately executes a functional
assertion and the fixture's unittest suite.

```text
python -B tools/live_smoke.py --provider all --case edit --port 9222 --max-turns 10 --json
DeepSeek: PASS (5 turns)
Qwen: PASS (4 turns)
MiMo: PASS (4 turns)

python -B -m unittest
Ran 224 tests
OK
```

The browser E2E exposed raw JSON protocol payloads and routine `[agent]` logs
in the chat stream. The UI now keeps those internal details out of the chat
and shows only turn dividers, tool rows, review state, and the final receipt.

## 0.1.6 Update

Version `0.1.6` adds a hidden, provider-neutral context handoff:

- one lightweight token estimate for every model
- a soft rollover point near 150k estimated tokens and a 200k hard budget
- one final hidden model turn that returns a bounded factual JSON summary
- a fresh model chat seeded with that summary
- local-fact fallback when summarizing fails
- no budget reset until the fresh chat's first handoff message succeeds
- no new UI controls, context meter, command, or compression notice

Automated verification:

```text
python -B -m unittest
Ran 210 tests
OK
```

Live verification:

| Flow | Result |
|---|---|
| DeepSeek chat summary -> fresh chat | Preserved two decisions |
| Qwen chat summary -> fresh chat | Preserved two decisions |
| MiMo chat summary -> fresh chat | Preserved two decisions |
| Qwen project summary -> fresh chat | Three edits completed; 3 tests passed |
| Qwen empty response recovery | Regenerated once and continued normally |

All project live tests used temporary directories. The main repository was not used as a model editing target.

## Previous 0.1.4 Update

Version `0.1.4` adds compact task receipts in the chat stream:

```text
DONE · 2 files changed · checks passed · restore available        View diff
```

The receipt is built from local facts, not model prose:

- changed file count comes from Git or snapshot changes
- `checks passed` comes from a successful local `run`
- `restore available` appears only for snapshot changes that can be restored
- `View diff` reuses the existing right-side changes drawer

Verification:

```text
python -B -m unittest
Ran 183 tests
OK

python -B -m unittest tests.test_receipt tests.test_agent tests.test_server tests.test_ui
Ran 82 tests
OK
```

Manual UI preview used a temporary local server that exercised the real send/SSE path. The chat showed the receipt line, `View diff` opened the drawer, and expanded diffs showed red/green lines with line numbers.

The 0.1.3 live provider smoke below remains the latest full DeepSeek/MiMo/Qwen live smoke pass.

## Scope

This pass reviewed Codey as a product system, not only as a unit-tested library:

- code elegance and duplication
- local unit tests
- single-model live smoke
- two-model writer/reviewer live smoke
- single-model self-bootstrap on a broken temporary Codey copy
- two-model self-bootstrap on a broken temporary Codey copy

All bootstrap tests used temporary copies. The main repository was not used as the repair target.

## Code Review

The current architecture is still reasonably clean:

- `agent.py` remains provider-independent.
- web page selectors stay isolated in provider drivers.
- two-model review lives in `review.py` and `task_runner.py`, without adding a group-chat UI.
- snapshot diff / restore stays separate in `changes.py`.

I did not extract the repeated `_visible_locator` helpers from MiMo and Qwen. They look similar, but keeping provider DOM code local is currently more robust: if one website changes, the repair stays in one adapter.

Small issues found and fixed:

- Qwen could edit a file and then try to finish without running tests, even when the task explicitly requested tests.
- Qwen could still echo website-side "tool does not exist" noise.
- MiMo could produce invalid JSON when an `old_string` contained unescaped quotes.

Fixes:

- `agent.py` now requires a successful `run` after file writes when the user asked for verification.
- `json_codec.py` now states earlier and more explicitly that tool names are local-runner JSON commands, not website tools.
- `json_codec.py` now reminds models to escape JSON strings correctly, or use full-file `content` when exact replacement strings are hard to escape.
- Added `tools/bootstrap_smoke.py` so self-bootstrap checks are repeatable.

## Local Tests

```text
python -B -m unittest
Ran 176 tests
OK
```

`git diff --check` passed.

## Live Smoke

| Mode | Result | Notes |
|---|---:|---|
| DeepSeek single create | Pass | Created code and tests, ran unittest, finished cleanly |
| MiMo single edit | Pass | Fixed bug, ran unittest, no upload-button misclick |
| Qwen single edit | Pass | After protocol fix, no "tool does not exist" noise; ran unittest before done |
| DeepSeek writer + MiMo reviewer | Pass | Writer fixed bug; reviewer approved |
| MiMo writer + DeepSeek reviewer | Pass | Reverse pair approved |
| Qwen writer + DeepSeek reviewer | Pass | Qwen entered review chain cleanly |

## Bootstrap Smoke

Bug injected into temporary Codey copies:

```text
difflib.unified_diff(after_lines, before_lines, ...)
```

Expected fix:

```text
difflib.unified_diff(before_lines, after_lines, ...)
```

| Mode | Result | Turns | Final full tests |
|---|---:|---:|---:|
| DeepSeek single | Pass | 5 | 160 OK |
| MiMo single | Pass | 5 | 160 OK |
| Qwen single | Pass | 5 | 160 OK |
| DeepSeek writer + MiMo reviewer | Pass | 5 | 160 OK |
| MiMo writer + DeepSeek reviewer | Pass | 5 after prompt fix | 160 OK |
| Qwen writer + DeepSeek reviewer | Pass | 5 | 160 OK |

## Does Two-Model Help?

For this specific injected bug, single-model repair was already strong: all three models fixed it in about five turns. The two-model path did not reduce writer turns because the bug was simple and tests were clear.

The advantage is confidence and failure recovery:

- reviewer sees the actual diff, not just the writer's summary
- reviewer can catch concrete mistakes before the user accepts the result
- if reviewer is unavailable, Codey falls back to single-model mode
- no extra UI switch is exposed to beginners

So the two-model feature is useful, but it should stay quiet and automatic. It is a safety layer, not a new product surface.

## Residual Risks

- Web pages can change DOM structure and break provider drivers.
- DeepSeek sometimes adds prose before JSON; the parser tolerates this.
- Web models can still be verbose or choose larger edits than a human would.
- Functional UI assertions and screenshot capture are automated, but there is
  no pixel-diff visual regression baseline yet.

## Passive Provider Supervisor and Writer Takeover

Deterministic local fault injection covers:

- typed provider action failures and stale-diagnostic clearing
- bounded health persistence, circuit cooldown, login/challenge state, and
  corrupt-state fallback
- data-free half-open canary with one-attempt behavior
- Writer takeover after edits using the current work checkpoint
- invalidating inherited green checks when a recorded file hash changes
- strict fresh chats, two-switch limit, shared turn budget, Stop priority, and
  first-rescue failure followed by a second sibling
- open-tab-first deterministic selection and health filtering for Doctor,
  hidden advisors, and Reviewer
- no takeover for ordinary local Python exceptions or agent stop reasons

Live Edge/CDP fault injection was run after the deterministic control-plane
tests. Each target's production message-box and send-button selectors were
replaced in memory, send-button heuristic selection was disabled, and a
healthy sibling selected among bounded DOM candidates. Recovery state used a
temporary store and did not modify the user's provider controls.

| Target | Sibling | Candidates | First recovery | Persisted reuse | Result |
|---|---|---:|---:|---:|---|
| DeepSeek | MiMo | 5 | 30.05s | 5.50s | provisional -> active |
| Qwen | DeepSeek | 2 | 45.20s | 6.08s | provisional -> active |
| MiMo | DeepSeek | 8 | 20.52s | 12.25s | provisional -> active |
| GLM | DeepSeek | 2 | 46.56s | 7.73s | provisional -> active |

The smoke exposed and closed two real recovery gaps before the final pass:

- Qwen could submit and receive a reply but lost the recovered send-button
  locator before its generation-complete check. Transaction-local locators now
  survive through staged validation and are cleared on success, abort, or
  explicit rejection; they are never persisted or reused across pages.
- GLM's send control is a `div.enter` without button semantics and was absent
  from bounded discovery. Discovery now admits only explicit send/submit/enter
  class candidates, with exact-token handling so `center` cannot impersonate
  `enter`.

## Release Notes

Version `0.1.3` is a durable model-browser release:

- model Edge/CDP browser is treated as long-lived user state
- Codey UI restarts do not intentionally close the model browser
- provider connections first reuse existing CDP browsers and model tabs
- if no usable CDP browser exists, Codey opens a new one automatically
- the last working CDP port is saved for quiet reuse after process restart
- no extra UI notification is shown for this recovery path

Version `0.1.2` was a usability and provider-status release:

- live green/gray model availability dots
- UI sends can still auto-open missing model pages
- connected models turn green after successful attach/open
- composer now uses Enter to send and Shift+Enter for newline
- root `DESIGN.md` is the single UI design source
