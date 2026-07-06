# Codey 0.1.17 Test Report

Date: 2026-07-07
Environment: Windows / Edge CDP reuse path / DeepSeek, MiMo, Qwen, GLM tabs open

## 0.1.17 Hidden MoA Layer

MoA is a hidden consultation layer, not a UI mode. When already-open
provider pages are available, Codey can use up to two other models as private
read-only advisors, then ask the selected model to synthesize one result.
Empty or placeholder-only projects can receive one hidden advisory plan before
the Writer starts. Existing projects use bounded read-only advisor audits before
the selected model acts; private audit reports are passed to the selected Writer
as advisory input.

Boundaries:

- Chat advisors cannot use tools, edit files, run commands, browse, or see the full project.
- Project audit advisors can only list, grep, and read selected project files.
- Dotfiles, env files, secret-like paths, excluded dependency/build directories, key/certificate files, lock files, binaries, symlinks, and oversized files are not shared with hidden project audit advisors.
- Project audit advisors cannot edit, run commands, request shell approval, or access paths outside the project.
- While a Writer tab is active, hidden advisors are borrowed from already-open sibling tabs instead of opening another CDP connection.
- Each project audit advisor has a bounded total time budget; unfinished advisors produce no private report.
- Advisor failures are ignored; the task falls back to the normal single-model path.
- If the selected model has already started hidden synthesis and that submission fails, Codey does not resend the original prompt.
- New Chat emits one ordinary assistant reply.
- Empty projects can receive one hidden plan before the selected Writer starts.
- Existing project audits are private reports; the selected Writer still verifies and decides.
- Project tasks that finish with `changed=False` can still refine the final read-only answer.
- Review remains a separate post-change acceptance layer over the final Diff.
- The web UI adds no buttons, modes, model-vote display, or group chat surface.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 416 passed |
| Consensus unit contract | Automatic advisor selection, bounded prompts, failures degrade |
| Server New Chat consensus | Agent loop not called; one `reply` emitted |
| New Chat synthesis failure | Original prompt is not resent after an uncertain hidden synthesis failure |
| Project audit advisor tools | Read-only file inspection works; attempted writes are rejected and files remain unchanged |
| Project audit secret boundary | `.env`, `prod.env`, credential files, excluded directories, symlinks, and secret search hits are not sent to hidden advisors |
| Project audit unfinished advisor | No `done(summary)` means no report is passed to the Writer |
| Sibling-tab advisor connection | Hidden consensus and project audit borrow already-open tabs from the active Writer context |
| Existing project audit | Private reports are injected into the selected Writer task |
| Existing project audit failure | Writer continues without the private reports |
| Empty project plan | Hidden plan is injected before Writer starts in a fresh chat |
| Project read-only consensus | Final answer can be refined after a no-change project task; no Review |
| Project read-only synthesis failure | Writer answer is kept and the provider session is forgotten |
| Project write task | Writer edits; Review still runs after Diff |
| Real Edge UI E2E | All 21 checks passed, including hidden consensus and existing restore/reconnect paths |
| Live Edge MoA project review | Passed with DeepSeek Writer plus MiMo and Qwen hidden audit advisors; two reports collected, no `.env` marker leaked, no files changed |
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
