# Codey iterative hygiene audit — final ledger

Base: `75964afe94ebaa35b401b3ae9ed7ad2b893e4a1b`; 2026-10-05 (Asia/Taipei).
Seven independent rounds completed; new candidate counts A/B/C/D/E/F/G are
3/3/3/3/0/0/0. Five behavior defects and one hygiene defect fixed, six candidates
excluded with evidence. Last two independent scans have no new reproducible
candidate. The one final full pytest ran only after convergence and all gates:
`7340 passed, 29 skipped, 1500 subtests passed in 477.50s (0:07:57)`, exit 0.
1170 production/test/tool source fingerprints are unchanged after the run.
Final summary and environment limits are in [TEST_REPORT](../TEST_REPORT.md).

## Method and gates

Independent rounds A production, B tests, C architecture, D UX/performance,
E reverse review, followed by call/state and fixture/fallback/cold-start
scans when a round finds no new issue. Every fix requires a red regression,
minimal implementation, affected tests, and new production/test/boundary scans.
Final full pytest is forbidden until all earlier convergence gates pass.
All gates passed before the single final run; after it only documentation edits
are permitted. Original audited implementation commit before documentation consolidation:
`6142f1a98a7e3e63d622cf7ab63935e5d10e7b2b`.
Successfully pushed to `https://github.com/waylliam1988/codey.git`, branch `master`,
exit code 0. Only report documentation changed after the final full pytest;
the run was not repeated.

## Candidates

| ID | Location | Reproduction | Failure evidence | Root class | Confirmed bug | Regression | Fix | Affected tests | Rescan |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A01 | codey/app/server.py:338 | pytest -q tests/test_http_short_body_never_dispatches.py | 200 != 400 for authorized short body and bootstrap | Production HTTP framing | Yes, fixed | Three cases including full-body control | Reject EOF before declared length, before dispatch/auth exchange | 99 passed (HTTP/auth/coldstart selection) | Done: body reader, routes, auth, coldstart, fixtures |
| A02 | codey/app/server.py:506 | pytest -q tests/test_desktop_http_listener_lifecycle.py | Live socket fd != -1 twice; child TimeoutExpired after 5s on Thread.start failure | Production lifecycle | Yes, fixed | Normal exit, warmup failure, independent-process thread-start failure | Close listener in both cleanup paths; shutdown only after thread starts; always release lease | 240 passed, 1 symlink skip, 6 subtests | Done: CLI UI entry, serve fallback, server tests, lock ownership |
| A03 | codey/storage/file_lock.py:214 | rg lease callers; pytest -q tests/test_file_lock.py tests/test_task_setup_cleanup.py | No current production cross-thread lease release reproduced | Design/unsupported ownership | No current bug confirmed | Existing contention/failure/writer reuse tests | None; headless close follows synchronous completed task, UI task finally releases its own writer | Included in 240-pass selection | Done; arbitrary cross-thread external use is residual risk |
| B01 | tests/test_architecture.py:49 | python -m pytest -q tests/test_architecture.py tests/test_completion_edit_integrity.py | AST: zero vacuous test functions; assert True matches are generated sample content | Test suspicion | No | Existing vacuous-test gate and edit-integrity tests | None | B-tests.log | Done |
| B02 | tests/test_fail_closed_runtime_sweep.py:55,184 | pytest -q tests/test_fail_closed_runtime_sweep.py | Both named owners exist and tests execute; no historical skip triggered | Test suspicion | No current bug | Huge page guard and metadata None tests | None; optional getattr branch is a future weakening risk | B-tests.log | Done |
| B03 | tests/conftest.py:12,26 | pytest -q tests/test_pytest_home_isolation.py tests/test_task_setup_cleanup.py | Home is isolated before imports and inherited by child; provider discovery fixture is opt-in | Test environment/design | No | Child-home and failed-entry cleanup checks; formal-entry tests | None | B-tests.log | Done |
| C01 | codey/ghost/affinity.py:777 (base) | pytest -q tests/test_affinity_coldstart_has_single_source_entry.py | Fresh child finds dormant _source_specs API (AssertionError) | Architecture hygiene/dead forwarding method | Yes, hygiene defect fixed | Child process checks single entry, real source conversion, persistence and reopen | Delete only unused private forwarding method | 122 passed, 5.89s (all affinity/ownership + hardening batch2) | Done: full AST, source owner, Ghost post-turn, app store, tests |
| C02 | codey/agents/shell_approval.py:339 and five other pairs listed below | python -m pytest -q tests/test_architecture.py tests/test_architecture_no_legacy_loops.py tests/test_affinity_sources_ownership.py | Six exact-body pairs; no second task/review/research loop found | Intentional leaf guards/domain wrappers | No behavioral bug | Existing owner/architecture/coldstart contracts | Retain short guards and event-log wrappers in their domain owners; no speculative cross-domain abstraction | C-boundaries.log | Done |
| C03 | codey/operations/task_loop.py:1; auto_loop.py | rg kernel callers; C boundary tests | auto routing dispatches shared kernel; test-only ResearchIteration adapter has no production owner | Architecture suspicion | No | No-legacy-loop, entry, research-facade, typed-request tests | None | C-boundaries.log | Done |
| D01 | codey/providers/local_discovery.py:79; browser_search.py:850,933; connector_search.py:522 | pytest -q tests/test_http_error_responses_close_on_failure.py | 12 failures: BytesIO response remained open for four transports x 401/403/500 | Production resource lifecycle | Yes, fixed | Keep HTTPError alive, verify closed stream and original error/skipped/auth verdicts | Close all non-redirect HTTPError responses; preserve bounded existing redirect cleanup | 197 passed, 7 subtests, 4.52s | Done: all five HTTPError handlers, redirect callers/tests, full AST |
| D02 | codey/research/browser_search.py:461,891 | pytest -q tests/test_browser_failed_pages_never_become_evidence.py | 4 failures: result.status was ok for blank/challenge HTTP and browser pages | Production structured status/evidence | Yes, fixed | Real acquisition + gateway/ledger; no evidence on failure; legitimate ERROR-prefixed article controls | Return canonical _fetch_failure with error detail; retain title/text/truncation | 217 passed, 7 subtests, 5.34s; later success controls included | Done: browser/HTTP/PDF status branches, gateway, tools, tests, full AST |
| D03 | codey/research/connector_search.py:219 | pytest -q tests/test_source_lifecycle_cancel_and_deadline_propagate.py | Corrected pre-fix run: DeadlineExceeded did not raise; 1 failed, 8 passed | Production cancellation/deadline | Yes, fixed | Formal search route; TaskCancelled propagates, DeadlineExceeded propagates, endpoint TimeoutError retains fallback | Propagate task deadline alongside cancellation; retain ordinary timeout fallback and finally restore search deadline | 214 passed, 7 subtests, 5.92s | Done: all search/fetch catches, search factory, source gateway, tests, full AST |

## Rounds

A completed (3 candidates, 2 fixes): inventory of all production packages, CI gates, task entry,
task_run lifecycle, headless cancellation/close, local provider history and
HTTP retry, event bus replay/overflow, operator authentication, HTTP body
parsing, desktop startup/shutdown, process capture, file-lock leases.

A answers: new A01–A03; A03 excluded by current production ownership and
writer tests; A01/A02 fixed with five red cases and one passing control.
New risk: shutdown-close ordering and rejecting truncated bodies; all three
post-fix scans completed and targeted lifecycle/HTTP tests pass. Next: tests,
fixture semantics, hidden skips, architecture and domain owners.
Additional A production selection: 514 passed, 3 skipped (one POSIX and two
Node unavailable), 30 subtests, 50.15s. No live provider involved.

B scan inventory: 375 production files (113088 lines), 761 test/support/manual
files (197196 lines), 15 tool files (5298 lines); AST parsed every file.
656 broad catches, 201 fallback/legacy/compatibility lines, 61 skip call sites,
zero vacuous test functions, one single-reference private facade and six
exact function-body duplicate groups. These are scan leads, not bug counts.
B answers: new B01–B03 all excluded by AST, executed tests and fixture/caller
evidence; no fix. Future risks: optional owner-presence skips could weaken a
later refactor; external browser/model gates are separate. Next: C follow the
unused facade and duplicate-body groups, kernel/dependency ownership.
B result: 211 passed, 400 subtests passed, 21.02s, zero skips.

C completed (3 candidates, 1 hygiene fix): full-tree AST/references, exact-body
duplicates, package dependencies, shared task kernel, auto routing, affinity
store/model/sources/events ownership and all live source conversion callers.
C01 removal evidence before deletion: `_source_specs` occurred only at its
private definition in production/tests/tools; sync already calls the owner
directly; ownership tests cover the supported public sync API; fresh-child
regression proves public sync and persisted replay survive without the facade.
No current caller, export or stored protocol uses the method. C02/C03 excluded
by domain ownership and executable architecture contracts. New risk: cold
store regression, covered by real source-to-persistence and reopen checks.
C02 exact locations (base line numbers; none changed): shell_approval.py:339
`_bounded_text` / knowledge/research_interest.py:361 `_clip` (bounded text);
ghost/hebbian.py:754 / ghost/inbox.py:854 `_read_events_unlocked` (same wrapper,
different domain event-log configuration); knowledge/note.py:238 / research/tools.py:506
`_as_float` (leaf scalar conversion); research/ledger.py:598 `_safe_page_number` /
research/object_model.py:82 `_safe_positive_page` (positive page guards);
research/plan_executor.py:317 / toolchain/tool_spec.py:359 `_policy_allows`
(fail-closed domain policy adapters); runs/ledger.py:490 `_int_or_none` /
runs/ledger_projection.py:460 `_optional_int` (durable-input/display conversion).
None implements a second runtime loop or state transition. Current callers
and strict-type/owner tests support retaining these small boundaries.
Next: D failures returned as successful evidence, HTTPError lifecycle,
connector deadlines and provider/CDP fixture isolation.
C boundary result: 486 passed, 444 subtests, 54.59s, zero skips.

D production/UX/performance scan: local discovery and physical request retries,
all five HTTPError catches, browser/HTTP/PDF redirect/failure/status contracts,
connector routing/cancellation/budget, gateway-to-ledger evidence, provider
catalog/cache/discovery fixtures, JS event/UI reconcile, external browser
isolation, storage/recovery state reducers and retained stress/process checks.
New D01–D03 all confirmed and fixed with separate post-fix production, test,
architecture scans. D01 had 12 red cases; D02 had four red cases. D03 initially
used `astronomy evidence`, which is not an arXiv-routing query; before touching
production, the fixture was corrected to `quantum astronomy evidence` and to
the authoritative `last_connector_errors` diagnostics. TaskCancelled and
endpoint TimeoutError then passed as controls; only DeadlineExceeded remained
red. No assertion weakened to accept a production failure.
New risks: failure-page status correction intentionally blocks formerly false
evidence; legitimate ERROR-prefixed content still succeeds. Error stream close
must preserve verdicts and redirects (affected tests cover both). Task deadline
must not eliminate endpoint timeout fallback (explicit control covers it).
Next E: reverse all changed callers, fixtures, cold entries and success/failure
transports; then independent state-flow and fixture/fallback rounds.
D UX/JS selection: 257 passed, 1 opt-in live-browser skip, 22 subtests, 11.13s.
Ruff and full-tree mypy pass (375 source modules).

E completed (0 new candidates): independently rescan all edited owners from
their callers: CLI/desktop/headless formal entries, bootstrap/auth/EOF reader,
listener start/normal/failure cleanup, all HTTPError verdict and redirect
paths, browser fallback to canonical gateway/ledger, search-factory and
connector task deadline vs endpoint timeout, Ghost post-turn sources to
event/persistence/reopen. New fixtures are file-local; shared conftest has no
changes. A full AST rescan found no vacuous tests and no single-reference
private facade; six small domain-owned duplicate groups remain unchanged.
Untouched storage/atomic writes, knowledge index, workspace revision,
runtime reducer, review orchestration, repairs and network policy were also
checked against owner boundaries and their failure contracts.
E answers: no new candidate, no new exclusions or fix; all six existing
confirmed issues remain resolved. Risks remain environment/live-provider,
heuristic challenge recognition and unsupported external lease ownership.
Reverse-entry/control selection: 275 passed, 22 subtests, 49.76s, zero skips;
diff whitespace check passes. Since this round found no new issue, it does
not end the audit: F is an independent caller/state-flow scan including real
process/crash/concurrency stress; G is an independent fixture/fallback/cold
scan including required machine contracts with Node.

Environment resolution: Node was absent from initial PATH, but the installed
Playwright driver provides Node v24.13.0. Subsequent commands prepend only that
directory to their process PATH; no dependency installation or skip added.

## Evidence storage

Raw commands and test outputs: ignored `artifacts/hygiene-audit/`.
Final report will reference checked-in regression names and exact statistics.

F completed (0 new candidates): fresh production import/call graph (1895
Codey import edges), state ownership from reserve/start/finish/release,
writer acquisition/release across task setup/finally and restore HTTP route,
runtime leaf reducer and pending intent/result delivery, workspace version
checks/atomic bump, immutable receipts, recovery replay/idempotence, Ghost
event replay, browser worker generations and shell Stop/Allow spawn gates.
The graph shows a single live `collect_source_specs` call in the store and no
`_source_specs` production call; five domain entries call the shared kernel,
two application adapters call the formal task submission. Runtime imports no
agents/operations/Ghost business owner. These static facts are corroborated by
entry/boundary contracts, not treated as whole-program proof.
Fresh test/architecture scan includes all tests/stress test files: actual
child kill/restart, crash-point matrix, multi-process locks, shell races,
SSE dedup/reconnect, soak/convergence and authoritative completion oracles.
Selection: 254 passed, 4 environment skips (two symlink, two O_NOFOLLOW),
34 subtests, 89.66s. No stress or independent-process check deleted or skipped
by this change. F answers: no new candidate/exclusion/fix, no newly introduced
risk; remaining risks unchanged. Next G independently tests fixture isolation,
fallback/compatibility inventories and fresh-process/required contracts.

G completed (0 new candidates): independently reread new file-local fixtures,
shared pre-import home isolation, supported aliases versus rejected old
fields, each fallback-bearing production module and all explicit skip sites.
Fresh-process tests exercise desktop startup failure without deadlock,
listener close/lease reuse and affinity source/persist/reopen, plus lazy
server/catalog import without external discovery. Acquisitions still pass
through canonical gateway/status/evidence; ordinary endpoint timeout and
legitimate ERROR-prefixed article controls remain green.
G cold/fixture selection: 69 passed, 6 subtests, 11.08s, zero skips.
Required machine contracts: 373 passed, 55.58s, zero failures/skips with
Playwright's Node v24.13.0. Fresh whole-tree AST still has zero vacuous tests,
zero single-reference private facades and the six retained leaf duplicates.
G answers: no new candidate/exclusion/fix or new risk. All confirmed issues
have pre-fix red regression evidence and passing affected tests; every excluded
candidate and every explicit skip/fallback has evidence above/below.
F found no new issue because the revised owners/callers preserve single-writer,
terminal, provenance and replay invariants under independent stress. G found
none because fresh entries and file-local fixtures use current production
owners, while failure and fallback controls exercise distinct verdicts.
F and G are different scans and are the most recent two independent rounds.
Next: mandatory Ruff/mypy/compileall/JS/collection/diff checks; only after those
pass is the one final full pytest authorized. After it, Python/JS/tests freeze;
only report documentation may change before commits/push.

## Skip/xfail inventory

Every explicit skip/decorator/SkipTest site found by a fresh test AST scan is
listed below, including skip-generation controls. No xfail/expectedFailure
site was found. Runtime skips must still be taken from final raw pytest stats.

| Site | Condition / call | Explanation |
| --- | --- | --- |
| tests/test_adapter_self_repair.py:787 | `self.skipTest('symlink creation requires privileges')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_adapter_self_repair.py:807 | `self.skipTest('directory symlink creation requires privileges')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_adapter_self_repair.py:826 | `self.skipTest('symlink creation requires privileges')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_atomic_io.py:83 | `unittest.skipIf(os.name == 'nt', 'POSIX executable bits are not stable on Windows')` | POSIX-only OS contract; platform guard |
| tests/test_atomic_io.py:234 | `unittest.skipIf(os.name == 'nt', 'POSIX permission bits are not supported on Windows')` | POSIX-only OS contract; platform guard |
| tests/test_atomic_io.py:77 | `self.skipTest(f'symlink creation unavailable: {exc}')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_atomic_io.py:198 | `self.skipTest(f'symlink creation unavailable: {exc}')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_audit_resolve_paths.py:44 | `self.skipTest('symlink creation unavailable')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_audit_resolve_paths.py:53 | `self.skipTest('symlink creation unavailable')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_audit_resolve_paths.py:62 | `self.skipTest('symlink creation unavailable')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_bounded_scan.py:71 | `self.skipTest(f'symlink creation unavailable: {exc}')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_cancellation.py:89 | `unittest.skipUnless(os.name == 'nt', 'Windows Job Object regression')` | Windows-only OS contract; platform guard |
| tests/test_cancellation.py:139 | `unittest.skipUnless(os.name == 'nt', 'Windows Job Object regression')` | Windows-only OS contract; platform guard |
| tests/test_cancellation.py:166 | `unittest.skipUnless(os.name == 'nt', 'Windows Job Object regression')` | Windows-only OS contract; platform guard |
| tests/test_cancellation.py:312 | `unittest.skipIf(os.name == 'nt', 'POSIX process-group contract')` | POSIX-only OS contract; platform guard |
| tests/test_changes.py:1261 | `self.skipTest(f'symlinks unavailable: {exc}')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_consensus.py:767 | `self.skipTest(f'file symlink unavailable: {exc}')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_fail_closed_runtime_sweep.py:59 | `self.skipTest('no _as_page')` | Conditional historical owner-presence guard; owners currently present and tests run; future weakening risk |
| tests/test_fail_closed_runtime_sweep.py:190 | `self.skipTest('no RunEvent/render')` | Conditional historical owner-presence guard; owners currently present and tests run; future weakening risk |
| tests/test_git_history_hygiene.py:38 | `pytest.skip('git worktree is unavailable')` | Requires Git/history; available in this checkout |
| tests/test_git_history_hygiene.py:42 | `pytest.skip(f'git log is unavailable: {result.stderr.strip()}')` | Requires Git/history; available in this checkout |
| tests/test_git_history_hygiene.py:55 | `pytest.skip('no release commit boundary found in history')` | Requires Git/history; available in this checkout |
| tests/test_git_history_hygiene.py:134 | `pytest.skip('git ls-files is unavailable')` | Requires Git/history; available in this checkout |
| tests/test_local_context_render.py:14 | `unittest.skipUnless(shutil.which('node'), 'Node.js is required for drawer behavior tests')` | Requires Node; resolved with installed Playwright Node for final gates |
| tests/test_local_context_render.py:70 | `unittest.skipUnless(shutil.which('node'), 'Node.js is required for drawer behavior tests')` | Requires Node; resolved with installed Playwright Node for final gates |
| tests/test_local_model_diagnostic_probe.py:50 | `unittest.SkipTest(f'live artifact {path.name} absent (no live run here)')` | Optional saved live-model artifact replay; synthetic analyzer controls also run |
| tests/test_manual_ab_harness_common.py:622 | `pytest.skip('git not available')` | Requires Git/history; available in this checkout |
| tests/test_project_config.py:131 | `self.skipTest(f'file symlink unavailable: {exc}')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_provider_connection_running_event.py:53 | `pytest.skip('Node.js unavailable')` | Requires Node; resolved with installed Playwright Node for final gates |
| tests/test_pytest_home_isolation.py:20 | `self.skipTest('windows only')` | Windows-only OS contract; platform guard |
| tests/test_research_comparison_benchmark_ab.py:431 | `pytest.skip('git is unavailable')` | Requires Git/history; available in this checkout |
| tests/test_run_command_semantics.py:247 | `unittest.skipUnless(sys.platform.startswith('win'), 'windows drive paths')` | Windows-only OS contract; platform guard |
| tests/test_run_command_semantics.py:260 | `unittest.skipIf(sys.platform.startswith('win'), 'posix absolute paths')` | POSIX-only OS contract; platform guard |
| tests/test_search_scan_split.py:80 | `self.skipTest('symlink creation unavailable')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_search_scan_split.py:207 | `self.skipTest('symlink creation unavailable')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_server.py:307 | `unittest.skipIf(shutil.which('git') is None, 'git is not installed')` | Requires Git/history; available in this checkout |
| tests/test_server.py:325 | `unittest.skipIf(shutil.which('git') is None, 'git is not installed')` | Requires Git/history; available in this checkout |
| tests/test_server.py:360 | `unittest.skipIf(shutil.which('git') is None, 'git is not installed')` | Requires Git/history; available in this checkout |
| tests/test_server.py:392 | `unittest.skipIf(shutil.which('git') is None, 'git is not installed')` | Requires Git/history; available in this checkout |
| tests/test_server.py:7239 | `unittest.skipIf(shutil.which('git') is None, 'git is not installed')` | Requires Git/history; available in this checkout |
| tests/test_server.py:7272 | `unittest.skipIf(shutil.which('git') is None, 'git is not installed')` | Requires Git/history; available in this checkout |
| tests/test_server.py:7053 | `self.skipTest(f'directory symlink unavailable: {exc}')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_shell_continuation_reports_denial_and_reconciles_ui.py:32 | `pytest.skip('Node.js unavailable')` | Requires Node; resolved with installed Playwright Node for final gates |
| tests/test_tool_runtime.py:70 | `self.skipTest(f'symlink creation unavailable: {exc}')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_tool_runtime.py:1310 | `self.skipTest(f'directory symlink unavailable: {exc}')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_tool_runtime.py:1327 | `self.skipTest(f'file symlink unavailable: {exc}')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_tool_runtime.py:1345 | `self.skipTest(f'directory symlink unavailable: {exc}')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_ui.py:1606 | `self.skipTest('node unavailable for executable JS behavior check')` | Requires Node; resolved with installed Playwright Node for final gates |
| tests/test_ui_browser_e2e.py:31 | `self.skipTest(f'real browser E2E is opt-in via {RUN_BROWSER_E2E_ENV}=1')` | Explicit opt-in live provider/browser E2E; retained |
| tests/test_ui_inplace_render.py:82 | `unittest.SkipTest(message)` | Browser unavailable outside CI; CI fails; helper also tested by intentional SkipTest control |
| tests/test_verification_policy.py:592 | `self.skipTest(f'file symlink unavailable: {exc}')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_workspace_paths.py:128 | `self.skipTest('O_NOFOLLOW unavailable')` | POSIX-only OS contract; platform guard |
| tests/test_workspace_paths.py:145 | `self.skipTest('O_NOFOLLOW unavailable')` | POSIX-only OS contract; platform guard |
| tests/test_workspace_paths.py:97 | `self.skipTest('symlinks unavailable')` | Filesystem symlink permission/capability; Windows lacks privilege |
| tests/test_workspace_paths.py:122 | `self.skipTest('symlinks unavailable')` | Filesystem symlink permission/capability; Windows lacks privilege |

## Fallback / compatibility inventory

A fresh scan covers every production file containing fallback/legacy/compatibility
(including documentation and embedded driver JavaScript). Keyword matches are
not automatically compatibility layers. The only removed forwarding API is C01,
with static, test, cold-start and no-supported-caller evidence above.

| File and scan lines | Current reason retained |
| --- | --- |
| codey/agents/shell_approval.py:223 | Historical input rejection/ownership documentation; shell approval rejects command-only records. Current canonical command events are supported. |
| codey/agents/state.py:13 | Historical input rejection/ownership documentation; shell approval rejects command-only records. Current canonical command events are supported. |
| codey/app/api.py:183,186 | HTTP/UI diagnostics and in-memory cache documentation; local OpenAI-compatible endpoint is a supported provider, not retired compatibility. |
| codey/app/http_plumbing.py:176 | HTTP/UI diagnostics and in-memory cache documentation; local OpenAI-compatible endpoint is a supported provider, not retired compatibility. |
| codey/completion/verification.py:13 | Verification display defaults and current applicability selection; legacy checks_passed is documentation of rejected provenance. |
| codey/completion/verification_map.py:69,90,180,184,185 | Verification display defaults and current applicability selection; legacy checks_passed is documentation of rejected provenance. |
| codey/completion/verification_policy.py:724,762 | Verification display defaults and current applicability selection; legacy checks_passed is documentation of rejected provenance. |
| codey/ghost/affinity.py:424,492 | Supported per-session/project/user scope derivation from current source specs; event projection replay/compaction ownership remains live. |
| codey/ghost/affinity_sources.py:172,277,433,434,439,440,442,444 | Supported per-session/project/user scope derivation from current source specs; event projection replay/compaction ownership remains live. |
| codey/ghost/event_projection.py:21 | Supported per-session/project/user scope derivation from current source specs; event projection replay/compaction ownership remains live. |
| codey/operations/auto_loop.py:97 | Current explicit executor dispatch, strict aliases/receipt rejection, canonical completion/display projections and policy-controlled provider switching; see detailed inventory below. |
| codey/operations/kernel_execution.py:156 | Current explicit executor dispatch, strict aliases/receipt rejection, canonical completion/display projections and policy-controlled provider switching; see detailed inventory below. |
| codey/operations/kernel_prompt.py:22 | Current explicit executor dispatch, strict aliases/receipt rejection, canonical completion/display projections and policy-controlled provider switching; see detailed inventory below. |
| codey/operations/kernel_protocol.py:28,348,349,350,351,444,467 | Current explicit executor dispatch, strict aliases/receipt rejection, canonical completion/display projections and policy-controlled provider switching; see detailed inventory below. |
| codey/operations/kernel_provenance.py:172 | Current explicit executor dispatch, strict aliases/receipt rejection, canonical completion/display projections and policy-controlled provider switching; see detailed inventory below. |
| codey/operations/kernel_recovery.py:123 | Current explicit executor dispatch, strict aliases/receipt rejection, canonical completion/display projections and policy-controlled provider switching; see detailed inventory below. |
| codey/operations/kernel_recovery_result.py:260 | Current explicit executor dispatch, strict aliases/receipt rejection, canonical completion/display projections and policy-controlled provider switching; see detailed inventory below. |
| codey/operations/project_adapter.py:126 | Current explicit executor dispatch, strict aliases/receipt rejection, canonical completion/display projections and policy-controlled provider switching; see detailed inventory below. |
| codey/operations/project_completion_checks.py:367,369,370 | Current explicit executor dispatch, strict aliases/receipt rejection, canonical completion/display projections and policy-controlled provider switching; see detailed inventory below. |
| codey/operations/project_writer_phase.py:33,209,226 | Current explicit executor dispatch, strict aliases/receipt rejection, canonical completion/display projections and policy-controlled provider switching; see detailed inventory below. |
| codey/operations/provider_preflight.py:25,28,35,68,114,163,181,188,218,230,231 | Current explicit executor dispatch, strict aliases/receipt rejection, canonical completion/display projections and policy-controlled provider switching; see detailed inventory below. |
| codey/operations/task_entry.py:554 | Current explicit executor dispatch, strict aliases/receipt rejection, canonical completion/display projections and policy-controlled provider switching; see detailed inventory below. |
| codey/operations/task_execution.py:315,316,317 | Current explicit executor dispatch, strict aliases/receipt rejection, canonical completion/display projections and policy-controlled provider switching; see detailed inventory below. |
| codey/operations/task_run.py:316 | Current explicit executor dispatch, strict aliases/receipt rejection, canonical completion/display projections and policy-controlled provider switching; see detailed inventory below. |
| codey/policies/action.py:55,249,400,401,406,408,413,415 | Current action guard and documented DNS TUN fake-IP policy; redaction word compatibility is vocabulary, not executable fallback. |
| codey/policies/network.py:9 | Current action guard and documented DNS TUN fake-IP policy; redaction word compatibility is vocabulary, not executable fallback. |
| codey/policies/redaction.py:90 | Current action guard and documented DNS TUN fake-IP policy; redaction word compatibility is vocabulary, not executable fallback. |
| codey/providers/capabilities.py:1,114 | Current local endpoint selection/configuration and bounded site DOM response/control ladders; supported provider drivers retain site-specific ordering. |
| codey/providers/controls.py:4,399 | Current local endpoint selection/configuration and bounded site DOM response/control ladders; supported provider drivers retain site-specific ordering. |
| codey/providers/local_config.py:110,411 | Current local endpoint selection/configuration and bounded site DOM response/control ladders; supported provider drivers retain site-specific ordering. |
| codey/providers/local_discovery.py:3,47,110 | Current local endpoint selection/configuration and bounded site DOM response/control ladders; supported provider drivers retain site-specific ordering. |
| codey/providers/local_openai.py:1,100,616 | Current local endpoint selection/configuration and bounded site DOM response/control ladders; supported provider drivers retain site-specific ordering. |
| codey/providers/web_drivers/mimo.py:401,402,403,405 | Current local endpoint selection/configuration and bounded site DOM response/control ladders; supported provider drivers retain site-specific ordering. |
| codey/providers/web_drivers/stepfun.py:97,100,205,208,209,214,223,230,237,250,251,258,283,287,290,295,296,352,361,363 | Current local endpoint selection/configuration and bounded site DOM response/control ladders; supported provider drivers retain site-specific ordering. |
| codey/research/browser_search.py:455,456,457,459,837,859,861,863,893 | Current browser/HTTP connector acquisition and structured failure, evidence merge/source-page/report/numeric defaults; no separate research loop. |
| codey/research/evidence_ledger.py:738,743 | Current browser/HTTP connector acquisition and structured failure, evidence merge/source-page/report/numeric defaults; no separate research loop. |
| codey/research/guards.py:51,53,55 | Current browser/HTTP connector acquisition and structured failure, evidence merge/source-page/report/numeric defaults; no separate research loop. |
| codey/research/identity.py:33,35,36 | Current browser/HTTP connector acquisition and structured failure, evidence merge/source-page/report/numeric defaults; no separate research loop. |
| codey/research/ledger.py:241,242,243,267,285,299,300,306,308,311 | Current browser/HTTP connector acquisition and structured failure, evidence merge/source-page/report/numeric defaults; no separate research loop. |
| codey/research/object_model.py:933,964 | Current browser/HTTP connector acquisition and structured failure, evidence merge/source-page/report/numeric defaults; no separate research loop. |
| codey/research/report_quality.py:480,482,489 | Current browser/HTTP connector acquisition and structured failure, evidence merge/source-page/report/numeric defaults; no separate research loop. |
| codey/research/source_gateway.py:3 | Current browser/HTTP connector acquisition and structured failure, evidence merge/source-page/report/numeric defaults; no separate research loop. |
| codey/research/tools.py:218,219,220 | Current browser/HTTP connector acquisition and structured failure, evidence merge/source-page/report/numeric defaults; no separate research loop. |
| codey/research/urls.py:4,15,17 | Current browser/HTTP connector acquisition and structured failure, evidence merge/source-page/report/numeric defaults; no separate research loop. |
| codey/runs/details.py:265,266,267,463,467,468,471,472,475,477,484,498 | Current durable trace/detail rendering and bounded diagnostics; legacy verification facts cannot manufacture trusted success. |
| codey/runs/trace.py:61,243,298,373,549,550,715,1336,1344,1350,1351,1352 | Current durable trace/detail rendering and bounded diagnostics; legacy verification facts cannot manufacture trusted success. |
| codey/runs/trace_schema.py:22 | Current durable trace/detail rendering and bounded diagnostics; legacy verification facts cannot manufacture trusted success. |
| codey/runtime/core/cancellation.py:165 | Windows Job termination fallback; strict canonical safe replay rejects aliases; prompt-source reference defaults are display identities. |
| codey/runtime/effects/safe_tool_replay.py:5 | Windows Job termination fallback; strict canonical safe replay rejects aliases; prompt-source reference defaults are display identities. |
| codey/runtime/observe/prompt_envelope.py:352,370 | Windows Job termination fallback; strict canonical safe replay rejects aliases; prompt-source reference defaults are display identities. |
| codey/storage/file_lock.py:196 | Lease ownership documentation and current safe managed-output filename default; no disk fallback cache. |
| codey/storage/managed_outputs.py:274,277 | Lease ownership documentation and current safe managed-output filename default; no disk fallback cache. |
| codey/toolchain/tool_spec.py:63,661,672,756 | Current tool schema + additional domain type-repair constraints; transport values are JSON-compatible, not old protocol support. |
| codey/utils/refs.py:57,63 | Bounded numeric/string leaf defaults and historical text-budget semantics; current production callers retained. |
| codey/utils/text_budget.py:16 | Bounded numeric/string leaf defaults and historical text-budget semantics; current production callers retained. |
| codey/workspace/change_brief.py:62,68,74,80,86,97,100 | Current no-Git snapshots, review/context display defaults, symbol hunk matching and rejection of legacy string verification/device paths. |
| codey/workspace/changed_symbols.py:129,195,213,230,244 | Current no-Git snapshots, review/context display defaults, symbol hunk matching and rejection of legacy string verification/device paths. |
| codey/workspace/changes.py:66,1306,1310 | Current no-Git snapshots, review/context display defaults, symbol hunk matching and rejection of legacy string verification/device paths. |
| codey/workspace/facts.py:156 | Current no-Git snapshots, review/context display defaults, symbol hunk matching and rejection of legacy string verification/device paths. |
| codey/workspace/paths.py:24 | Current no-Git snapshots, review/context display defaults, symbol hunk matching and rejection of legacy string verification/device paths. |

Concrete fallback ownership and executable evidence:

- Provider preflight/writer failover: current `provider_preflight` callers from
  task lifecycle/writer phase, supervisor bounded attempts and action guard;
  provider preflight, writer failover, provider-ID-policy and flow tests.
- Tool executor fallback: injected executors take precedence; canonical
  project/source/knowledge dispatch owns remaining tools; shared kernel,
  coldstart boundary, policy closure and typed-request tests.
- Project non-repository snapshot: only `not_repo`/missing Git trigger it;
  other Git failures stay failed; changes, readonly cleanup and setup tests.
- Recovery absent proof: ordinary non-kernel events may bump workspace;
  corrupt present proof never downgrades; frame unsafe rows reject. Recovery
  ownership/strict conversion/trusted proof/workspace tests cover the split.
- Source HTTP fallback: one caller after unusable DOM content; current
  non-browser HTTP/PDF consumers remain; D01/D02 failure/control regressions,
  redirect/private-target, source gateway and cancellation tests cover it.
- Connector/browser fallback: real PubMed/arXiv sources remain supported;
  endpoint timeout keeps browser fallback, task lifecycle exceptions abort;
  source connector tests and D03 controls cover it.
- Site response/control ladders: StepFun head-first DOM ordering and MiMo
  normalization remain current adapters; their driver and send-loop tests
  execute ladders. Live site DOM drift is residual risk, not removal evidence.
- Schema legacy key allowlists are extra current domain-validation shapes;
  the canonical kernel still rejects old search/replace edit fields before
  execution. Tool args/spec/generic schema/coldstart tests cover rejection.
- Research fallback source/note/report fields are current model-produced
  structured inputs, sanitized and bounded; ledger/object-model/record merge
  and report-quality tests cover them. These do not add a second task loop.
- Remaining numeric/text/hunk/scope defaults and unavailable-display rows are
  bounded domain projections with live callers; they do not manufacture
  success, receipts or authorization. Owner/strict-type/identity tests remain.
- DNS fake-IP compatibility is a documented current TUN policy tradeoff;
  literal fake IPs remain blocked. Network policy tests cover both forms;
  browser DNS rebinding cannot be ruled out by this application-level guard.
