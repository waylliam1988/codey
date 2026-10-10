# Iterative review audit — 2026-10-10

This is the audit ledger for the convergence review. `CHANGELOG*` remains
unchanged; `TEST_REPORT.md` is updated only after the convergence gate.

## Candidate table

| ID | Location | Reproduction / evidence | Root cause | Confirmed bug | Regression | Fix | Affected tests | Rescan |
|---|---|---|---|---|---|---|---|---|
| A-001 | `codey/app/api.py:88-119` | `pytest -q tests/test_api*`; provider probe failure returns HTTP 200 with `probe_error=true` and empty statuses | intentional fail-closed availability fallback | No; behavior is asserted by provider/API response tests | existing API/provider tests | none | 886 targeted provider/API/task/headless tests pass | yes |
| A-002 | `codey/providers/api_transport.py:149-207` | SSE cancellation, deadline, malformed terminal frame, and byte cap cases | transport uncertainty policy | No; tests preserve unknown outcomes and prevent replay | existing transport/recovery tests | none | provider timeout/API transport tests pass | yes |
| A-003 | `codey/app/headless_runner.py` and `codey/operations/task_loop.py` | cold-start/headless/task entry behavior and cancellation paths | shared production execution spine | No; architecture and headless/task tests cover the same entry path | existing headless/task tests | none | 886 targeted tests pass | yes |
| B-001 | subprocess/UI tests with one outer `returncode` assertion | inspected 8 single-assert subprocess tests; each embeds behavioral assertions inside the subprocess script | test style candidate | No; inner scripts assert state transitions, ordering, and payloads | existing tests | none | relevant UI/auth/lifecycle tests pass | yes |
| D-001 | `tests/test_operator_bootstrap_before_ui_requests.py:16-38`; `tests/test_sse_browser_dedup_reset_and_buffer_gap.py:13-51` | `pytest -q` produced 5 `FileNotFoundError: [WinError 2]` failures when invoking `node` on this host | test environment precondition missing | Yes, test portability bug | same tests now skip explicitly when `shutil.which("node")` is absent | added explicit Node.js capability skips and used resolved executable | targeted set: 7 passed, 15 skipped; architecture rescan: 178 passed, 415 subtests | yes |
| D-002 | `tests/test_opencode_compaction_reference_replay.py:16-31`; `tests/test_pi_compaction_reference_replay.py:22-46` | reference replay subset produced 5 `FileNotFoundError: [WinError 2]` failures from the Node-based frozen-source runner | test environment precondition missing | Yes, test portability bug | same tests now skip explicitly when Node.js is unavailable | added explicit Node.js capability skips | reference replay subset: 2 passed, 5 skipped; post-fix architecture rescan: 140 passed, 393 subtests | yes |

## Round log

### Round A — production behavior

Scanned production entry points, task loop, provider/API transport, headless
runner, cancellation/retry/close paths, broad exception handlers, and cold
start architecture boundaries. Evidence: 8,414 tests collected; `ruff check
codey tests` passed; `compileall -q codey tests` passed; architecture/cold-start
smoke: 107 passed and 383 subtests; provider/API/task/headless targeted set:
886 passed, 10 skipped, 164 subtests.

New candidates: A-001, A-002, A-003. Confirmed bugs: 0. Excluded candidates
are documented above with behavior-test evidence. No production fix was needed;
therefore no TDD mutation was required in this round. Next round audits test
quality, fixture semantics, skips, and mock assertions.

### Round B — test quality (pending)

Scanned all test ASTs for vacuous `assert True`, exit-code-only tests,
unjustified `skip`/`xfail`, mock-heavy tests, fixture mutation of production
semantics, and missing failure-path assertions. There are no executable test
functions whose only assertion is `assert True`; the eight outer
`returncode` assertions inspected all contain independent behavioral
assertions in their subprocess scripts. All dynamic skips have explicit
environment or capability reasons (Node.js, Git, symlink/permission support,
OS-specific process contracts, or opt-in browser E2E). No `xfail` markers were
found. The quality/fixture/architecture subset passed 1,196 tests and 455
subtests.

New candidates: B-001 (the eight subprocess return-code wrappers), already
excluded in the candidate table after inner-script inspection. Confirmed bugs:
0. No production or test changes were necessary. Next round audits static
callers, compatibility layers, duplicate loops, and cold-start ownership.

### Round C — architecture and compatibility (pending)

Static import/call scanning (with package-prefix resolution) produced seven
modules without direct static importers: `codey.__main__`, the behavioral probe
worker, Local/Zen connection adapters, the provider worker child, protocol
diagnostics, the mutation queue, and OpenAI tool lowering. Each has an explicit
cold-start or dynamic boundary: `__main__`/worker children are process entry
points; provider connections are catalog-loaded by string; protocol
diagnostics is imported by the research test adapter; and the queue/OpenAI
lowering modules are imported by architecture and native-tool tests and
runtime discovery. The architecture suite also proves there is one task loop,
one evidence-followup owner, no retired compatibility files or importers, no
runtime dependency cycles, and no dead convenience exports.

New candidate: C-001 (static importer leaves). Call-graph, dynamic-entry, and
test evidence disproves dead code; confirmed bugs: 0. The compatibility and
cold-start subset passed 1,139 tests and 454 subtests. No deletion or repair is
justified. Next round checks UI/user-visible errors, waits, repeated provider
connections, external-resource isolation, and performance-sensitive paths.

### Round D — user experience and performance

Scanned UI/SSE reconnect and deduplication, provider refresh/retry and
connection reuse, browser worker lifecycle, headless waits, cancellation, and
external provider/CDP isolation. The first deterministic run exposed D-001:
five browser subprocess cases failed before executing because Node.js is not
installed. This was a test-only environment precondition omission; no
production path was reached. The minimal fix added explicit `Node.js
unavailable` skips, then the affected tests passed (7 passed, 15 skipped).
Post-fix production/test/architecture rescans passed: Ruff clean, and 178
architecture/cold-start/UI tests plus 415 subtests passed. No duplicate send,
unbounded retry, or user-visible state mismatch was confirmed. Remaining
Node.js absence is recorded as an environment risk, not convergence evidence.

New candidate: D-001 (fixed). Confirmed production bugs: 0. Next round is the
repair reverse review from callers, fixtures, fallbacks, and cold-start paths.

### Round E — repair reverse review

Rechecked every caller of the affected UI assets and operator-auth/SSE entry
points, including server boot, HTML script ordering, SSE reconciliation,
composer retries, operator HTTP fixtures, and the headless/desktop shared
services. The new `shutil.which("node")` guard only controls the subprocess
test precondition; it does not alter production code, request ordering, auth,
SSE, fallback, or fixture data. `git diff --check`, `compileall`, and the
affected UI/auth/SSE tests passed (18 passed, 15 explicit skips). The
architecture/UI rescan found no new import, ownership, or dead-code issue.

New candidates: 0. Confirmed bugs remaining: 0. D-001 remains fixed. Because
this round found no new problem, the next two scans are intentionally
independent: one follows state/call relationships, and one follows test
fixtures, fallbacks, and cold-start behavior.

### Round F — state flow and caller graph

Followed the active-run owner from HTTP submission through `RunRegistry`,
`AppContext`, provider status, headless projection, task submission, the single
`run_task_kernel`, SSE replay/reconciliation, shell approval continuation, and
terminal settlement. A loop scan found no second task/review/research loop;
the additional loops are bounded provider/worker or UI reconnect mechanics.
The state-flow regression set passed 287 tests, 8 explicit environment skips,
and 24 subtests. New candidates: 0; confirmed bugs: 0. This is the first of
the two required independent no-new scans after Round E.

### Round G — fixtures, fallbacks, and cold-start paths

Independently rescanned fixture helpers, reference replay adapters, Node-based
subprocess boundaries, fallback/compatibility strings, cold-start imports, and
vacuous-test patterns. The first scan found D-002: five OpenCode/Pi reference
replay tests lacked the same Node.js precondition guard as the UI tests. After
the minimal test-only fix, the reference subset passed (2 passed, 5 explicit
skips); production/test/architecture rescans passed Ruff and compileall plus
140 tests and 393 subtests. No fallback was bypassed, no fixture changed
production semantics, and no new dead or duplicate implementation appeared.

New candidate: D-002 (fixed). Confirmed production bugs after repair: 0. The
post-fix scan produced no new stable candidate.

### Round H — recovery, concurrency, cancellation, and idempotence

Ran an independent state-flow review over pending/unknown provider effects,
stop and cancellation paths, recovery delivery, duplicate settlement,
browser-worker generations, SSE reconnects, and process cleanup. The focused
stress and lifecycle set passed 49 tests with one explicit platform skip; the
additional fail-closed/legacy architecture checks passed 39 tests and 22
subtests. Static scans found no new unbounded retry, duplicate settlement,
resource leak, or second owner. New candidates: 0; confirmed bugs: 0. This is
the first clean scan after the post-G repair.

### Round I — final independent hygiene scan before convergence

Re-ran test collection, test AST quality checks, Node subprocess call-site
inspection, fallback/compatibility references, and the full mypy tree. Results:
8,414 tests collected; no vacuous test functions; 15 skip/xfail call sites with
explicit platform, capability, or opt-in reasons; all Node subprocess tests
either resolve an executable or skip; mypy reports no issues in 407 source
files. Ruff, compileall, and `git diff --check` remain clean. New candidates: 0;
confirmed bugs: 0. Rounds H and I are the two most recent independent scans
without a new stable candidate, satisfying the no-new convergence rule.

## Convergence result

All required pre-final gates passed: Ruff, mypy, compileall, test collection,
diff check, and affected tests. The unique final full pytest run passed **8384
tests, skipped 30, and reported 1503 subtests in 1013.92s (0:16:53)**. No
production or test files were changed after that run. The documentation-only
commit hash and push result are added here after commit.

## Risks and environment limits

- Browser/UI tests that require Node.js or browser drivers may skip when the
  dependency is unavailable; each skip must retain an explicit environment
  reason.
- External providers and CDP are not treated as convergence evidence; their
  probes remain isolated from deterministic unit and stress tests.
