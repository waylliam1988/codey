# Codey Test Report

This file records the current verification state, not an append-only experiment diary.

## CodeMode values, answer repair and nested JSON boundaries (2026-10-25)

The preceding rg implementation was committed as `7f31d11d`. The changes below
are the subsequent repair work.

CodeMode success display now derives `{"ok":true,"value":...}` from the same
program value as `canonical.value`, with separate result-data and metadata
sections in the shared native/text renderer. Primitive strings no longer appear
as an unexplained JSON string followed by prose. Literal content quotes, empty
strings, null, booleans, numbers and containers retain their original values.

The original tool requirement evaluator now returns one ephemeral result used
for completion status, proof reason and repair feedback. It distinguishes absent
execution/parent, invalid receipt, unavailable selection, conflicting observations,
invalid answer JSON, missing answer fields, wrong types and wrong values. Once
execution and observation are satisfied, feedback requests only answer repair,
shows bounded expected/actual JSON and identifies the committed receipts. It
requires bare JSON without Markdown fences. No expected-answer cache, second
execution loop, result coercion, quote stripping or compatibility reader was added.
Explicit `read_tool_result(view="value")` pages the full canonical value from the
same receipt; missing values fail instead of substituting display text. Default
output reading retains the existing managed-output integrity checks.

Raw HTTP evidence located the text quoted-query failure before provider parsing:
a missing backslash closed the outer JSON string early. The parser correctly
rejected it. CodeMode's frozen ToolSpec now supplies a quoted-text JavaScript
example and its JSON request generated from that same program string. Strict
JSON diagnostics share bounded source location and escaping guidance; malformed
requests are never guessed, unescaped or executed. A recorded bad response is a
regression fixture, including repair through the original task loop and exact
quoted arguments reaching the actual packaged rg backend once.

An independent worker boundary regression found Node readline splitting raw
U+2028/U+2029 inside JSONL messages. The existing Python-to-worker writer now
uses ASCII JSON encoding, preserving decoded Unicode data while protecting frame
boundaries. Both source input and host-result regressions exercise the real worker.

Verification passed: **268 focused tests and 63 subtests**, then **96** focused
receipt/answer regressions after adding canonical-value reading, and **72** final
tests after clarifying bare-JSON feedback. These overlapping selections are not
summed as distinct tests. Coverage includes both protocols, actual worker/rg
execution, strict whole-batch rejection, invalid JSON constants/overflow, literal
quotes/backslashes/control characters, long-value paging, forged display text,
conflicting observations, missing parents/receipts and restart recovery. Ruff,
mypy (522 production files), Pyrefly (0 errors, 29 existing suppressions, 80 warnings)
and ty passed. Architecture checks passed **97 tests and 405 subtests**, excluding
the separately observed existing line guard: unchanged `operations/task_loop.py`
remains 1231 lines against its 1205 ceiling, identical to HEAD. No ceiling changed.

The final local Gemma4-12B native/text matrix passed **12/12** across read, quoted
literal search, ordinary literal search, regex, empty results and Stop. All answers
were checked against committed child observations and repeated recovery. The
quoted-query case took two generations in native mode and three in text mode;
text repaired a malformed `done.summary` JSON request without repeating grep.
Both Stop cases started no subsequent generation. An earlier text read run also
repaired an unwanted trailing newline by changing only done, without rereading.
The pre-fix quoted-query failure and intermediate native Markdown-fence failure
are retained; successful samples do not erase those model limitations.

Final raw requests/results, receipts and complete production source archive:
ignored `.e2e-artifacts/answer-and-quoted-final-release-20261025/`. All **524**
Python/MJS source hashes match the delivered source and archive. Focused test logs
are `answer-and-quoted-final-focused.log`, `answer-value-view-final-tests.log` and
`answer-and-quoted-final-delta.log` under `.e2e-artifacts/`. Full pytest and the
entire machine-contract selection were not run, as requested.

## One packaged rg backend for grep (2026-10-25)

The existing `grep` entry now uses one `rg_search` engine. Normal project search
and audit search both supply their authorized file lists to it; both Python
content-matching implementations were removed. Existing workspace containment,
excluded directories, symlink rules, audit secret filtering and file/byte budgets
remain at admission. Hidden and gitignored files stay searchable within that
scope. Explicit-file arguments bypass rg's glob filters, so the positive relative
POSIX `include` glob is applied at admission. Default matching remains literal
and case-insensitive, with explicit regex and case-sensitive options.

`ripgrep-bin==15.2.0` is a pinned runtime dependency with platform hashes in the
generated CI lock. The executable resolves only through that distribution's file
record: no PATH search, runtime download or Python search fallback. JSON queries
use fixed argv, `--no-config` and the existing cancellation/process-tree owner;
argument batches share one elapsed-time budget. Full match text and byte-column
coordinates live in `canonical.value`; previews, pagination and coverage are
derived views. The original frozen schema drives CodeMode declarations/validation.
Canonical pending replay arguments now preserve mode, case, filter and pagination;
its allowed fields derive from the same tool definition.

Regression probes caught and fixed native-platform edge cases: explicit-file glob
bypass, `--no-encoding` restoring BOM autodetection (now `--encoding=none`), JSON
records containing Unicode line separators, LF/CRLF versus trailing CR, and
preservation of new options in pending intents. Malformed/partial JSON, missing or
inconsistent summaries, exit-status mismatches, raw-output overflow, invalid
patterns, matching non-UTF-8 lines, missing binaries and process failures cannot
become successful empty searches. Stop and timeout tests verify the original
process owner reaps the child and closes pipes. Admission omissions and continued
pages cannot claim `scan_complete=true`.

Final focused verification passed **379 tests and 81 subtests (80.92s)** across
search, audit admission, both protocols, CodeMode, lifecycle, lineage, answer
bindings, receipt/restart integrity, safe replay and dependency boundaries. The
new rg contract file contains **27** cases. Ruff, mypy (522 production files),
Pyrefly (0 errors, 29 existing suppressions and 80 warnings) and ty passed. The
existing machine-contract selection includes the new regression file.

The current wheel was built and installed into an independent venv with the
pinned rg wheel. With PATH empty, the installed package completed regex search,
resolved its own venv executable and validated the generated runtime inventory.
Evidence: ignored `.e2e-artifacts/rg-wheel-smoke.json`, build/install logs and wheel.
This installed-runtime smoke was performed on Windows; other platforms were not
executed here.

The final local Gemma4-12B native/text matrix passed **7/8** across literal search,
regex search, empty results and Stop. Both regexes, both empty searches, text
literal search and both Stops passed, including repeated receipt recovery. Stops
started no following generation. Native literal search is retained as a failure:
all actual grep observations contain exact `PATTERN.*`, but the model encoded it
again as a quoted string in the final JSON, then repeated searches until the
original no-progress guard stopped it after six generations. The answer binding
rejected that wrong content; no result coercion, source rewriting or fallback was
added to force the case green. The earlier development matrices are also retained.
Final evidence is under ignored `.e2e-artifacts/rg-live-release-20261025/`, including
raw requests, receipts, the complete production source archive and 524 Python/MJS
hashes matching the delivered source. Wheel production source matches as well.

Architecture checks passed **97 tests and 405 subtests**, with the known line
guard checked separately: unchanged `operations/task_loop.py` is still 1231 lines
against its 1205 ceiling. No ceiling was raised. Full pytest and the entire machine
contract selection were not run, as requested.

## Structured read results and explicit answer bindings (2026-10-25)

The `A/C` failure now has two controls in the existing runtime. Read results carry
one canonical page object; frozen ToolSpec output schemas drive CodeMode's actual
model declarations, catalog digest and host result validation. Missing structured
values fail explicitly rather than falling back to display text. Successful text
tools have a declared string contract. Child values retain their declared type;
artifact conversion remains at the parent output boundary.

Optional answer selectors live in the existing immutable `required_checks`, with
the original tool/argument/parent selectors. The existing completion gate compares
strict final JSON with committed full receipt values or fully observed absolute
file lines. It never rereads files, reexecutes tools, stores expected answer text,
or treats excerpts as authoritative. Restart reconstructs the same receipt cache.
Failed attempts and unrelated pages cannot prove a line; they do not poison a
valid observation. Conflicting successful observations block completion.

New regressions cover `A/C` rejection, exact answers before/after restart, missing
or substituted cached receipts, escaped JSON pointers, duplicate keys, invalid
JSON/types, empty/Unicode/CRLF text, clipped lines, pagination and conflicting
observations. Both protocols exercise the production task loop rejecting the wrong
answer and accepting correction without repeating tools. The new files are part
of the existing machine-contract selection; no separate runtime was introduced.

Final focused verification passed **239 tests and 21 subtests (73.87s)** across
CodeMode, lifecycle, lineage/recovery, receipt integrity, completion authority,
immutable snapshots, recursive schema validation, paging and project tools.
The final read-contract file, including two additional strict-boundary checks,
also passed **18/18 (2.73s)**. Together the two new files contain 39 passing cases.
Ruff, mypy (521 production files), Pyrefly (0 errors, 29 existing suppressions,
80 warnings) and ty passed.

Architecture checks passed **97 tests and 405 subtests** with one known failing
line-count guard deselected. That guard still fails because unchanged
`operations/task_loop.py` has 1231 lines against its 1205 ceiling; `git show HEAD`
has the same 1231 lines. The changed read producer is within its existing 1440-line
ceiling. No architecture ceiling was loosened, and the full machine gate was not
rerun or claimed green.

The final local KoboldCpp Gemma4-12B native/text read/write/Stop matrix passed
**6/6**. Both read answers contain complete values; repeated recovery is stable,
and Stops start no subsequent generation. Both final reads completed in two model
generations. Text writing exposed invalid model-written JavaScript, corrected
through the original loop on its third generation. An earlier retained native
read exposed missing return and wrong-field `.split` errors and recovered on its
sixth generation. No generated source was rewritten. Raw evidence is retained under ignored
`.e2e-artifacts/answer-contract-live-verified-20261025/`; the source manifest binds
all 523 production Python/MJS files. The earlier two six-case runs are retained
separately, including the boundary change between runs.

This is explicit observation consistency, not a general semantic answer oracle.
An entry must declare its answer bindings; execution-only requirements retain
their existing meaning. Full pytest remains reserved for the user's evening run.

## Unified tool execution lifecycle (2026-10-25)

Normal leaves and CodeMode children now use the same session-local admission in
`execute_turn`. Parallel eligibility comes from the frozen ToolSpec; other leaves
remain exclusive through workspace identity and receipt commit. Admission owns
no persistent outcomes. Existing receipts remain authoritative during Stop and
recovery. Linked cancellation propagates the original Stop and earliest deadline
to host threads. Parent return, failure, timeout and Stop join admitted callbacks;
unawaited tool/state calls cannot produce a successful parent. Cancellation retains
committed child references through the normal parent settlement. CodeMode state
writes use only the existing task checkpoint; worker/result write mirrors were
removed without a compatibility reader or another runtime.

The initial new regression reproduced successful parent return before its child
finished. Final focused verification: **157 passed, 1 platform skip (60.70s)** across
admission, cancellation/process cleanup, CodeMode, durable child lineage, restart,
snapshots, immutable results and research projections. Acquisition-failure tests
also verify worker and pipe cleanup. The new lifecycle regression file is included
in the existing required machine-contract selection. Ruff, production mypy
(520 files), Pyrefly (0 errors, 29 existing suppressions and 80 warnings), ty and
all 54 discovered JavaScript/MJS syntax checks passed.
After making the threaded test rendezvous explicit, the final lifecycle/cancellation
selection also passed **24 tests, 1 skip (9.24s)**. The skip is the POSIX process-group
contract on Windows. All 522 production Python/MJS hashes in the final live source
manifest still match the delivered source.

Local KoboldCpp Gemma4-12B was exercised with real native and text-protocol model
requests, the production kernel/tools/receipts, and repeated fact recovery. The
initial six-case read/write/Stop matrix passed. After retaining cancelled-parent
child references and final worker cleanup, the final matrix passed **5/6**:
both writes, both Stops and text reading. Stops produced no following generation,
kept files unchanged and restored the same failed parent/child facts.

The final native read case is retained as a failure: actual read receipts contain
`AMBER-471` and `COBALT-923`, but the model explicitly used `a.value[0]` and
`b.value[0]`, returning `A/C`. Execution and recovery checks passed; independent
answer checking failed. A required successful read does not validate arbitrary
answer semantics. Text write runs also exposed invalid model-written JavaScript,
which failed explicitly before the model corrected it; no source rewriting or
fallback was introduced. Final evidence and source hashes are under ignored
`.e2e-artifacts/lifecycle-live-final-20261025/` (summary, requests, receipts and
source manifest); the initial matrix is retained separately.

The broader machine selection stopped at an existing Research test fixture failure:
`test_chat_research_tools_continue_shared_kernel_without_mode_redispatch` expects
an open result while its fake returns no required canonical research observation.
An independent `git archive HEAD` checkout at `45f12e28` reproduces the same failure
(`.e2e-artifacts/lifecycle-baseline-45f12e28/baseline-result.txt`). No production
fallback was added to satisfy the obsolete fixture. This is not a green machine
gate. An initial broad run also identified fact-copy tests trying to deep-copy
the new admission resource; those tests now preserve execution-resource identity,
and the relevant Research/prompt regressions passed.

Full pytest was started, then stopped at the user's request for their evening run;
it has no complete result. Its partial log is
`.e2e-artifacts/lifecycle-full-pytest-20261025.log`. No full coverage or complete
release matrix is claimed.

## Retry isolation and lifecycle contract cleanup (2026-10-23)

Native URL and error-document presentation now retain the initiating startup
trace. A late exception is logged against that attempt and cannot fail or
replace a later retry's page or cancel its deadline. Four deterministic threaded
regressions cover blocked URL/error-document delivery, timeout/retry and a new
attempt that is either navigating or ready.

The dispatcher always uses its module-owned frame coalescer and active-run map.
The former sliced-source status test now runs the full published application in
headless Chromium. Snapshots project durable task states through the existing
lifecycle transition; owner-registry rows no longer synthesize unversioned starts
or idle completions, and the older-server cursor fallback was removed. A browser
regression checks that the registry cannot invent or settle a task independently.
Required shell-result expiry and approval-queue cancellation are direct calls;
provider transport abandonment remains optional. Tests verify cancellation order
and that missing required wiring raises instead of silently skipping cleanup.

Verification: **129 startup/cancellation/provider/UI regressions passed (158.93s)**;
**50 frontend files / 277 tests passed**; **62 UI architecture/source/SSE checks
passed**; **26 real wheel/source-archive/runtime/cache tests passed (187.97s)**.
The broader server/approval/cancellation selection reported **289 passed, 1 skipped,
1 failed**: an unchanged scheduler-close test exceeded its 2-second dispatch-start
wait during concurrent runs. Its complete file immediately passed on isolated
rerun (**2 passed, 1.12s**); this initial timeout is retained here rather than
reported as an all-green broad run. A subsequent cancellation/lifecycle/approval
selection including that test also passed (**27 passed, 14.13s**).

Follow-up investigation used independent real SQLite databases: 30 serial and
160 concurrent scheduler trials all dispatched and closed; 5 exceeded two seconds
(maximum 4.68s), with timeout stacks in SQLite commit/connection cleanup. In an
alternating 120-trial comparison, the existing connection-per-operation path had
4/60 readiness timeouts; holding an idle connection had 1/60. Its p95 readiness
fell from 2.38s to 0.086s, but COMMIT still took up to 2.70s and final connection
close remained slow. A subsequent 160-trial FULL/FULL-with-anchor/NORMAL-with-anchor/
memory control run had no two-second readiness timeouts in any group, confirming
the intermittent nature rather than establishing a deterministic speed guarantee.
The existing store opens/closes connections for each operation; last-connection
WAL cleanup can amplify storage latency while the durable session line is held.
These results identify a production latency concern, not a reproduced lost-wakeup
or deadlock, and cannot prove the cause of the original uninstrumented timeout.
No production connection or durability policy was changed in this patch.

The scheduler readiness test now allows ten seconds and emits scheduler-thread
identity, liveness and stack on failure without querying the possibly blocked
store. Its existing two-second cancellation-signal and one-second close/join
assertions remain intact. The focused startup/abort/lifecycle selection passed
again (**47 passed, 18.87s**). The complete broader selection was then rerun
and passed (**290 passed, 1 skipped, 311.87s**).

Repository Ruff, full-production mypy (512 files), Pyrefly (0 errors; 29 suppressed,
80 existing warnings), ty, TypeScript, Biome, production build, 54 JavaScript syntax
checks and frontend publication consistency passed. Python type gates explicitly
targeted 3.11. No complete repository pytest or coverage run was performed.

## Installed runtime and independent native startup (2026-10-23)

Source publication and wheel construction now produce the same `codey/runtime.json`
contract. Installed startup, snapshot hashing, dependency comparison and validation
copies consume that descriptor instead of a repository `pyproject.toml`. The source
archive retains its frontend/test inputs and includes its in-tree build backend;
obsolete wheel build files are removed before publication. No legacy-runtime
reader or data migration branch was added; this transition is tested with fresh state.

Native service preparation starts from the host's background callback, independently
of `loaded` or synchronous JS feedback. The coordinator joins service readiness and
host visibility. Deadline phase tokens, one preparation gate and attempt identity
cover close, retry, delayed callbacks and timer-thread failure. Feedback is embedded
as escaped text in the startup document; presentation does not hold the service gate.
Older pytest assumptions were updated to this contract.

Verification selections (overlap): **96 release/startup/runtime/ownership tests passed
(165.64s)**; **236 server and UI tests passed (244.32s)** including Stop, Continue,
approval recovery and native launch integration; **50 frontend files / 277 tests
passed**. Real wheel install in an isolated environment outside the checkout passed
cold start, same-instance reconnect, upgrade and cold restart. Source-archive build
also passed (**2 installation/archive tests, 144.32s**); the final complete source
archive was checked again after preserving its frontend CSS and pytest inputs
(**1 passed, 45.43s**). Additional startup/inventory/cache failure cases passed.

A real hidden WebView2 window, real isolated service and full application reached
`desktop.interface_ready` with no coordinator `loaded` callback in **10.24s** under
concurrent test load. This is a cold-start functional sample, not a speed comparison.
The user's prior idle local service was cold-switched to the new descriptor format.
Repository Ruff, production mypy/Pyrefly/ty, TypeScript, Biome, published frontend
metadata and production build were checked. No full repository coverage run was made.

## Frontend publication fingerprint (2026-10-23)

The CI publication mismatch was reproduced from the committed source bytes.
The publisher had mixed LF/CRLF locally, while Git stored LF; the old metadata
hashed the local bytes and therefore failed on a fresh checkout. Text build
inputs now fingerprint CRLF as LF. Binary inputs and published assets still
use exact bytes, so normalization cannot accept modified runtime output.
Publication checks now report stale metadata, source fingerprints and missing,
extra or changed files instead of exiting silently.

The production rebuild changed only the metadata source fingerprint, with no
JS/CSS asset changes. Publication validation passed both locally and against
temporary LF-normalized source inputs. The frontend build/typecheck, shipped
JavaScript syntax (54 files), Ruff and 64 focused publication, UI architecture
and CI configuration tests passed. Full-suite and actual remote CI results
remain outside this local verification.

## Linux CI runner compatibility (2026-10-23)

The supplied Linux CI log failed before tests: Playwright 1.58.0 could not
install Chromium on the Ubuntu 26.04 environment selected by ubuntu-latest.
The Linux job now pins ubuntu-24.04, retaining the hashed Python dependencies
and browser installation checks. All 14 existing CI configuration tests passed.
Actual Linux browser installation remains to be verified by the next CI run.

## Full-suite failure triage (2026-10-23)

Reviewed the supplied **14 failed / 9681 passed / 59 skipped** run. Twelve
failures used outdated UI/prompt contracts or incomplete test hosts: exact main
class matching, the old search button, direct access inside collapsed process
groups, the former busy-effort rule, copy-button hover styling, prompt hashes,
and a Node VM missing the browser AbortSignal API. Raw document/workspace HTTP
fixtures now attach and close the explicit public event feed used by the service.
Tests exercise current user navigation and retain streaming, single-execution,
permission revocation, history recovery and accessibility assertions.

Historical prompt comparison reproduced both the original initial-prompt hash
and its replacement: the difference is the external-fact evidence paragraph
introduced in 2fbb6fb3. Result and repair hashes remain unchanged.

The scheduler timeout reproduced under repeated Windows SQLite startup. In 100
instrumented cycles every task dispatched exactly once; three exceeded 2 seconds
(maximum 2.59 seconds), including delays inside start_task's commit. The restart
test retains exact dispatch/ownership checks, uses a 10-second eventual-progress
window and reports checkpoint/in-flight/blocking state on failure. This evidence
does not indicate a lost scheduler wakeup.

The remaining architecture failure was actual file growth. Approval CAS folding
and physical abort cleanup moved to cohesive helpers without changing commit,
locking or cancellation behavior. Existing file-size ceilings remain intact;
no legacy write path, compatibility projection or runtime fallback was added.

Validation: **651 passed, 5 skipped, 405 subtests** for durable, architecture,
effect-sandwich and current regressions; **26 passed** for the final focused
SSE/prompt/readability/approval/live-browser selection; **3 passed** for streaming
and workspace browser checks; **1 passed** for the document/command preview.
Selections overlap. Ruff, mypy (511 production files), Pyrefly and ty passed;
Pyrefly retains 80 warnings and 29 suppressions with zero errors. The full
repository suite and Linux validation were not rerun locally.

## Recovery and startup review follow-up (2026-10-23)

First contact and retries now use the same UI acceptance rule. Cached revisions
remain unconfirmed until the current server is observed; missing baselines and
reset stores recover current drafts only for surviving chat identities. Tests
cover immediate success, repeated read failure, reset revision, remote deletion
and typing while restoration is in flight.

One accepted task transition updates all loaded views referencing that task,
including inherited history whose owner is absent from the sidebar. Stop ownership
remains with the source. Older events cannot revive settled inherited work.

The standalone pond script catches entry/dependency load failures independently
of the main module. One native readiness deadline also covers missing startup
code or a stalled document. Ready, failure and close cancel it through one
attempt-fenced completion path. Recommendation generation/decoding and unused
startup progress callbacks were removed rather than retained as compatibility APIs.

Validation: **24 restoration/startup tests passed (31.36s)**, **103 backend,
provider, release/cache and lifecycle tests passed (54.68s)**, and **50 frontend
files / 277 tests passed**. The complete server module also passed **208 tests
(187.27s)**. The wider browser run passed **117 cases**; its new
legacy-cache case initially seeded storage before pagehide overwrote it. The
fixture now seeds the next document, and the complete affected 24-case selection
passes. Browser selections overlap. Stop, admission, approvals, Continue, edit,
draft saving and workflow regressions were included. Repository Ruff, production
mypy (**509 source files**), Pyrefly and ty, TypeScript, Biome, production build,
published metadata and shipped-JavaScript syntax checks passed. Pyrefly retains
80 existing warnings and 29 suppressions with zero errors. No full repository
suite or coverage run was made; native UI was not operated.

## UI state and task history (2026-10-23)

Empty-chat creation preserves an enabled explicit model selection and independent
thinking preferences, otherwise exposing `Choose model`. Directory order and
connection defaults cannot implicitly select a disabled model. UI metadata uses
one versioned synchronization entry point and a single atomic local cache envelope.
Conflicts reconcile pending edits, and server-owned deletion identities prevent
resurrection even after a creation response was lost. Clear-history preserves the
session and draft. Generic `Not saved` feedback is removed; transient errors retry
silently, while sustained draft failures name the affected draft explicitly.

History pages and focused windows read task checkpoint versions from the existing
durable store. They use the same lifecycle projection as live events. Tool history
alone cannot imply an active task; an answer cannot override a Running checkpoint.
Inherited fork history never assigns a source task's Stop ownership to the child.
No task runtime, task store or persistent lifecycle display-event copy was added.
See [ownership and synchronization](docs/ui-state-and-history.md).

| Check | Result |
| --- | --- |
| Backend, history, metadata, edit and control regressions | **135 passed, 6 subtests; 56.62s** |
| Complete server test module | **208 passed; 205.01s** |
| Core shipped-page browser chains | **115 passed; 153.11s** (deletion, drafts, choices, restart/history, Stop, Continue, edit, reading and settings) |
| Expanded UI failure regressions and related coverage | **77 passed; 106.73s** (overlaps the core UI selections) |
| Frontend | **50 files / 272 tests passed**; TypeScript, Biome, production build and published metadata passed |
| Python static checks | Repository Ruff and full-tree mypy (**509 source files**), Pyrefly and ty passed for **3.11, 3.12 and 3.13**; Pyrefly retains **80 existing warnings and 29 suppressions**, with zero errors |

Regression cases include deleting the last chat, model disabling, clearing history,
concurrent fields, edits during acknowledgement, stale mutation responses, cache
migration, reconnect, a lost creation response followed by remote deletion, failed
and cancelled task recovery, unowned checkpoint cancellation, stale lifecycle events,
fork ancestry, and final-answer/checkpoint ordering. Expanded tests use real metadata
stores and valid durable message identities instead of production compatibility hooks.
The previous full-suite result below remains historical; this work did not rerun
all repository tests or measure coverage. Native UI was not operated.

## Desktop startup (2026-10-23)

Latest follow-up: normal startup now displays the shared character pond without
progress copy, covers restoration, and fades out after frontend readiness. Safe
error text and Retry remain. Healthy recovery snapshots with identical verified
content attach without another preparation. Composer model/effort controls stay
enabled after boot, including during Sending and Running; queued requests capture
their choices before asynchronous metadata saving.

Follow-up verification: **53 browser/startup/Stop/Continue tests passed (73.53s)**,
**49 release/cache/publisher/startup tests passed (6.68s)**, and **50 frontend files /
272 tests passed**. These selections overlap. TypeScript, Biome, production build,
published metadata, repository Ruff and production `codey` mypy/Pyrefly/ty passed.
Unscoped Pyrefly/ty also inspected tests and reported pre-existing test typing
errors; those are not zero-error gates. No full-suite or new coverage run was made.
Native UI was not operated. The latest existing local warm-start log recorded
**3.09s** from window creation to interface readiness, including **1.02s** for service
attachment; this is an observed sample, not a promise of instant cold startup.

Earlier startup implementation verification follows:

The native desktop now creates its startup window before shared-service work.
The same window navigates to the application; startup and interface boot failures
retain an English reason and Retry. Static validation evidence is reusable only
for matching runtime, interpreter, dependency and validation-rule fingerprints.
Each new preparation still backs up the current databases and runs the isolated
service preflight. Existing startup serialization, activation journals and rollback
remain authoritative.

| Check | Result |
| --- | --- |
| Python focused regressions | **145 passed, 24 subtests; 159.49s** across release control/materialization, real listener lifecycle, activation journals, frontend build, startup/cache/fingerprint boundaries, chat edit/Stop/Continue and cold-start cleanup |
| Frontend | **48 files / 261 tests passed**; TypeScript, Biome, production build and published metadata passed |
| Python static checks | Repository Ruff passed; full-tree mypy (**508 source files**), Pyrefly and ty passed for Python targets **3.11, 3.12 and 3.13**; Pyrefly reports **80 existing warnings and 29 suppressions**, separately from its zero-error gate |
| Startup boundaries | Cache corruption, environment/rule/source invalidation, failed/mutating validators, optional-cache write failure, fresh database evidence on cache hits, single-flight Retry, concurrent service attachment, close during startup and stale frontend readiness callbacks covered |
| Window verification | Fake native lifecycle verifies window creation/loading precedes service preparation; headless Chromium verifies the pond, safe error text and Retry in the same document; the user's desktop was not operated |

An isolated real preparation sample took **12.454s** without static evidence and
**10.044s** with a cache hit. The cached attempt skipped compilation/CLI execution
but still copied and checked the candidate and ran a fresh database preflight.
These samples measure preparation only, under concurrent test load; they do not
measure native first paint, cutover or full application readiness. New bounded
startup timing logs distinguish those phases on subsequent desktop launches.
This section is focused verification, not a rerun of the historical complete suite.

## Complete suite (2026-10-22)

The complete Windows pytest run passed with **9486 passed and 59 skipped** in
**3105.06s (0:51:45)**. The skips are intentional legacy or environment-gated
contracts; pytest completed without failures.

This run includes the immutable local-release startup path, frontend
materialization, concurrent startup serialization, model-preference read/write
locking, and the real multi-session browser regressions.

## Zen catalog lifecycle (2026-10-22)

Deprecated directory entries are filtered on disk-cache load, fresh discovery
and new admission. Settings projects the raw cached directory locally, including
names for unavailable selected IDs, without rewriting consent. Manual Refresh
models bypasses the five-minute TTL and retains ETag validation; automatic
discovery skips disabled sources and the open UI checks API catalogs hourly.
Settings, the picker and Send use the same catalog; old chats and staged choices
survive refresh. Older status/catalog replies cannot restore retired entries.

| Check | Result |
| --- | --- |
| Focused Zen/model/connection/frozen regressions | **939 passed, 8561 deselected, 4 subtests; 51.81s** (`pytest tests -k 'zen or connection_status or model_preferences or model_settings or models_projection or api_connection_removal or dynamic_api_model or headless_api_model_selection' --ignore=tests/manual`) |
| Browser lifecycle, model management, source actions, save/focus and catalog tests | **41 passed; 66.95s** |
| Additional disabled-discovery/input validation and cold-start checks | **21 passed; 3.80s** (overlaps earlier selections) |
| Frontend | **42 files / 178 tests passed**; TypeScript, Biome, production build and published metadata passed |
| Python production static checks | Full-repository Ruff and full-tree mypy (**498 source files**, Python targets 3.11 and 3.12), Pyrefly and ty passed; Pyrefly reports 76 unnecessary-type-conversion warnings outside the modified Python files and 29 suppressions separately from its zero-error gate |
| Windows native UI | Production backend and pywebview/WebView2, isolated QA profile: real official catalog download and conditional 304; selected retired models hidden from picker, named in Settings; old chat/draft retained and Send disabled; injected metadata outage retained rows/choices; recovery retained staged Beam selection; explicit Save updated picker |

New regressions were observed failing before implementation (five backend and
five browser cases). Iteration also caught and fixed subscription cleanup,
disabled-source refresh timing, malformed source responses and empty nested
disclosure interference with existing focus/layout contracts. Native QA made
metadata requests only; it did not run inference or change the user's profile.
Local QA records are under `.e2e-artifacts/zen-catalog-live/`.
This is a focused regression pass, not a new complete-suite or coverage result.

## External control v0.3 verification (2026-10-21)

This pass implements the shared local service, delegation/review loop, desktop
chat synchronization, RPC/MCP, bounded public feed, immutable release switching,
and dynamic Zen endpoint discovery. Usage and limits are in
[the implementation guide](docs/external-control-implementation.zh-CN.md).
The package version remains unchanged; v0.3 is the external-control design version.

| Check | Result |
| --- | --- |
| Complete pytest, no failure cap | **9226 passed, 5 failed, 59 skipped, 1515 subtests; 2970.15s (49:30)** |
| Required machine contracts after SQLite initialization fix | **1129 passed, 14 subtests, no skips; 792.03s (13:12)** |
| Focused regression after all five full-run failures | **11 passed; 48.99s** |
| Additional UI and documentation-contract checks | **49 passed; 3.44s** |
| Frontend | **42 files / 178 tests passed**; TypeScript, Biome, production build and published metadata passed |
| Python production static checks | Ruff, mypy (**495 source files**), Pyrefly and ty passed before the final test/document-only migration |
| Current coverage | Not measured for this worktree; baseline coverage below is historical |

The complete run was allowed to finish before fixes. Its five failures were:

- Two real browser approval/retry cases and one live shell-output case: their
  raw HTTP fixtures omitted the public feed now owned explicitly by LocalService.
  Fixtures now attach/detach the production event feed; no old SSE path was restored.
- Approval HTTP restart: an obsolete continuation-submission mock and an active
  model scheduler made the fixture race canonical wakeup. The fixture now observes
  the original pending task without dispatching model work, and asserts the same
  task/submission identity and resolve-shell-approval input, with no second task.
- Provider selector DOM harness: its textarea stub lacked Element.before, used
  by the composer. The stub now follows the production DOM API.

The focused run covered all five failures plus adjacent tests. An intermediate
fixture edit also added cleanup to the model peer by mistake; its NameError was
fixed before the final 11-pass run. Per the user's instruction, **no second full
suite is run after these fixes**. GitHub CI will establish the post-fix complete
suite and coverage; this report does not claim that result in advance.

Other completed checks during implementation: focused backend **268 passed,
6 subtests**; durable/architecture/task/HTTP **568 passed, 5 skipped, 399 subtests**;
real-process release **7 passed**; Zen suites **90 passed**; real conversation
browser and external-control **38 passed**; after the SQLite fix, targeted
**70 passed** and architecture/startup/ownership **103 passed, 399 subtests**.

New production defects were first reproduced red: queue metadata and source
identity loss, terminal checkpoint leaks before native preparation, pause/drain
races, release validation/rollback identity issues, browser authentication loss
on replacement, late task_start replay reviving a terminal spinner, dropped
Research/elapsed fields, and concurrent SQLite WAL initialization. The last one
now serializes complete cold initialization with the existing OS-backed lease;
normal transactions and their write authority are unchanged. Tests using removed
HTTP globals, queue writers or SSE internals were migrated to current entry points.

Earlier capped full attempts (before the user prohibited failure limits) ended at
3/2167, 5/2180 and 5/3315 failed/passed, mainly on stale fixtures. The third attempt
also hit scheduler recovery's 2s wait and approval admission's 250ms timing checks
while machine/full suites ran concurrently. Both passed serial focused reruns;
assertions were left intact. These are recorded flakes, not claimed production
fixes. The final machine and complete suites above ran serially, and those two
checks passed. Interrupted machine attempts preceded source/bundle changes and
are not counted as passes.

Real-model verification used isolated state and public Zen models: initial
`greeting.txt` and its linked refinement were independently checked byte for byte.
The refinement reached max_turns; file acceptance is distinguished from normal
model termination. No shell/install was delegated. Real automatic switching
preserved the service port and database identity, and the browser remained
authenticated and restored chat/model selection. Evidence is retained locally in
ignored `artifacts/external-control-live/`, including `simple-run.json`,
`visible-chat.jpg`, complete-run JUnit and focused JUnit; credentials/runtime
state are not committed. The observed page is the shared browser frontend;
native WebView rendering is not claimed as visually verified.

This is Windows/Python 3.12 verification. Other Python versions and fresh
post-fix coverage remain CI work. Automatic release switching requires unchanged
runtime dependencies and database schema; no arbitrary migration rollback,
throughput ranking, or token-saving percentage is claimed.

## Pre-external-control baseline (2026-10-21)

| Check | Result |
| --- | --- |
| Final complete pytest | **9145 passed, 59 skipped, 1515 subtests; 3178.34s (52:58)** |
| Final required machine contracts | **1129 passed, 14 subtests; no skips** |
| Production statement coverage | **88.21%**; CI minimum **85%** |
| Production branch coverage | **77.33%**; CI minimum **75%** |
| Coverage scope | All **478** production Python files; Python subprocesses included |
| Python static checks | `mypy codey`, `pyrefly check codey`, and `ty check codey` passed; `pyrefly check` over tests/tools still reports the repository's existing unannotated test/tool baseline |
| Ruff / diff check | passed |
| TypeScript / Biome / Vitest | **42 files / 177 tests passed; typecheck, build and published metadata check passed** |
| Real HTTP protocol continuation | **22 passed**, using deterministic local peers for both native protocols |
| Real HTTP Stop stress | **100 passed**: 50 before headers, 50 during silent error bodies |
| Transport regression group | **64 passed**; final implementation also passed under coverage |
| Conversation A-F live browser integration | **5 new tests plus 16 adjacent live/browser tests passed** against the packaged UI and isolated real Codey HTTP service |
| Post-static-check regression group | **230 passed** after Ruff/mypy/Pyrefly/ty cleanup |
| Installed wheel UI | Actual HTTP and Chromium; all 4 SVG icons loaded, Reload retained scroll position, no page errors |
| JavaScript syntax / published assets | passed; actual wheel and source archive contents verified |

Local runtime validation uses Windows and Python 3.12.8. The other Python versions
were checked as static-analysis targets; their runtime suites run independently in
GitHub CI. Each Windows matrix entry now runs the full suite once in the Pytest
step with coverage collection, followed by combination, reporting, gating and
version-specific artifact upload. Browser boot failures include page, console,
request and restoration diagnostics.

Confirmed defects were reproduced with failing regressions before fixing them:
missing SVGs in distribution archives, document Reload losing its reading position,
approval expiry removing pending state before durable persistence, failed expiry
leaving claimed tickets reusable, missing result receipts being hidden by older
continuation failure state, and Windows Stop blocking on buffered network I/O.
The 2026-10-16 conversation UI pass additionally reproduced and fixed explicit
input rejection locking the composer, historical Research cards opening the
latest report, Markdown repairs changing inline code, and local images silently
stopping after the twentieth image. The four frontend regressions were first
red, then green; the packaged UI was verified through the real Codey HTTP server
with Playwright.
Real HTTP/browser checks cover approval restart, Allow/Deny, continuation retry,
workspace document preview and sidebar actions. The final A-F integration also
covered structured provider failures and fixed recovery actions, canonical final
answer and message copying, question draft refresh recovery, a real approved shell
showing flushed output before process exit, local formula/Mermaid rendering, and
archive pagination/search. Three sessions were run concurrently with session-scoped
Stop while each session remained serial internally. The browser used the packaged
`codey/web` assets, an isolated state directory, and deterministic local model
peers; no external model API was called and `E:\Selfie - 副本` business files were
not modified.

The live shell observation contract is intentionally bounded: execution identity,
incremental UTF-8 capture, bounded stdout/stderr snapshots, and same-card
`Running` to `Ran` updates are verified. The browser currently polls snapshots on
demand; there is no dedicated `execution_output_available` SSE stream. Cross-origin
or cross-profile draft migration, all Mermaid graph types, a complete virtual list,
and recovery of history that was discarded before archive introduction are not
claimed.

The final static-check pass was also red before cleanup: Ruff reported 18 findings
(imports, dead locals and a redundant exception form), mypy reported 2 type errors,
and Pyrefly reported 2 inference errors. The smallest source and test fixes made
Ruff, mypy, Pyrefly and ty pass; the affected backend, approval, HTTP and live
browser regression group then passed **230 tests**. The initial ty invocation hit a
Windows ACL error while reading `codey/web`; after restoring the current user's
read access, the same `ty check codey` command passed.

Coverage reports were generated from the final complete-suite capture and its
parallel subprocess data. The full-production coverage gate and JSON/XML/HTML
generation passed. The deterministic concurrency and durable-recovery tests are
included in that capture.

This change also verified a clean Python 3.12 environment from the generated
`requirements-ci.txt`: hash-checked installation, editable project installation
with `--no-deps --no-build-isolation`, and `pip check` all passed. CI contract
tests confirm both Windows and Linux jobs consume that lock without
reresolving. The EOF stability cases (including slow startup), watchdog timeout,
approval HTTP restart, browser approval, and continuation lifecycle regressions
all passed. The migrated Allow/Deny response reconciliation and conversation UI
regressions run in Vitest against the real frontend modules; the Python bridge
tests and enum shims were removed.

The 59 skips are explicit environment, platform, or separately gated integration
cases. The required machine-contract gate has no skips. The actual HTTP/Chromium
approval and document regressions run normally in the complete suite.

Installed-package UI evidence is under ignored
`.e2e-artifacts/review-20261015/`. Coverage reports are under
`.e2e-artifacts/coverage/`.

Live Zen matrix: **36/42** initial cases passed; four of six failed cases passed on retest. Two Ling thinking-enabled cases remain unresolved. The free-model matrix is not all green. This review used local deterministic peers rather than new live-provider calls; its results do not establish live-provider health or release status.

## 2026-10-17 response-latency regressions

The current continuation pass added two independent red/green regressions. Holding
`AppContext.lock` in a test first made `model_settings.settings_response()` hang;
removing that long read-side lock made the same test return 200 while the lock was
held. A browser regression then held `/api/state` open before Send; the old packaged
UI never reached `/api/run`, while the updated composer reached the authoritative
admission endpoint immediately. The accepted-run draft clear remains after the
HTTP acceptance boundary, so a lost response or newer edit is not cleared.

The new regression file `tests/test_current_regressions.py` passed **2/2**. The
focused model, approval, and draft groups passed **75/75**. The frontend browser
regression `test_send_admission_does_not_wait_for_slow_state_reconciliation` passed
after rebuilding and publishing `codey/web`. The two-approval lifecycle regression
also passed; no backend worker serialization bug was reproduced.

Static checks for the changed production paths passed: Ruff, mypy, Pyrefly on
`codey`, ty, TypeScript, and Biome. A full `pyrefly check` still fails on existing
unannotated tests and tools; that failure is recorded rather than hidden. The
packaged asset refresh came from `npm run build` followed by
`python tools/publish_frontend.py`; no external model API or Selfie business file
was used.

## 2026-10-21 deterministic concurrency-test cleanup

The final pass replaced timing-only assertions in scheduler redispatch, process
output observation, browser-worker health, file-lock contention, and shell Stop
linearization tests with events, explicit release points, and direct reconciliation
steps. The scheduler abort budget was widened only for the test's bounded teardown
path; production timing and behavior were unchanged. Two durable recovery tests
that had been skipped because they used removed JSONL fixtures now construct the
current durable effect records and execute as real regressions. The run-event
characterization now asserts the existing `call_id` field, and the architecture
line ceilings were updated to the measured current values.

Focused regression checks passed before the final run. The final complete suite
then passed **9145/9145**, with **59** explicit skips and **1515** subtests. Coverage
combination, JSON/XML/HTML generation, and the full-production gate passed at
**88.21% statements** and **77.33% branches**. The five untracked `collect*.txt`
and `file_ranges.txt` files were confirmed to be unreferenced historical analysis
outputs and removed. After the report update, the affected regression selection
passed again: **187 tests and 399 subtests**.

## Reports and reproduction

- [Transport, CI and MiMo diagnosis](docs/provider_ci_diagnosis_2026-10-13.zh-CN.md).
- [UI, maintenance and removable-connector audit](docs/ui_maintenance_audit_2026-10-13.zh-CN.md).
- `python -m tools.machine_contract_gate`: required deterministic contracts; skips fail the gate.
- `python -m pytest -q`: complete suite.
- `python -m coverage run -m pytest -q`, then `python -m coverage combine`, `python -m coverage json`, `python -m coverage html`: statement and branch measurement. Use an absolute `COVERAGE_FILE` under `.e2e-artifacts/coverage` when subprocesses change directory.
- `python -m tools.coverage_gate`: both coverage minima and the full-production file inventory.
- `python -m tools.check_javascript`: syntax of the actual shipped JS/MJS assets.
- `python -m tools.source_size_inventory`: tracked and non-ignored new source files across production, tools, tests and CI.
- Full coverage setup and report commands: [test index](tests/README.md).

## Historical records

[Complete report before consolidation](https://github.com/waylliam1988/codey/blob/3ed30494830aeb937409b76ae20f59650183e8dc/TEST_REPORT.md). All prior 407 report sections remain in Git history.

Keep generated full logs and coverage HTML/JSON/XML under ignored `.e2e-artifacts/`, and upload them as CI artifacts. Commit concise conclusions and reproducible commands. Existing `docs/reports` evidence is retained because linked experiments still use it; do not append new raw bulk output there.
