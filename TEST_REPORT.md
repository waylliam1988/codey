# Codey Test Report

This file records the current verification state, not an append-only experiment diary.

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
