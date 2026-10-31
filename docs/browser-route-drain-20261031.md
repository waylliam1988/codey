# Research browser route drain, 2026-10-31

The earlier ignoreErrors teardown (`fa797f9a`) did not wait for active route
handlers. Two subsequent complete Research attempts still emitted unobserved
TargetClosedError futures. PYTHONASYNCIODEBUG captured their creation in
Playwright Page._on_route's background network-interception-pattern update.
Session.close disconnected pw.stop() while those updates were still pending.

BrowserSearchProvider now calls the public unroute_all(behavior="wait") API
before disconnecting. No exception filter, dependency patch or shared-tab close
was introduced. Duplicate search/fetch page references are still released once,
and close is idempotent.

Real production-path verification:
`artifacts/research-ownership-minimal-fixed-20261031`,
run `run_c5c18f90a0b8480bb65a2677712a269a`, searched twice, fetched and paginated
the official SQLite WAL page, saved a finding and completed three assessment
attempts with asyncio debug enabled. Shutdown produced no TargetClosedError or
unobserved-future warning. The unrelated Node DEP0169 and debug slow-handle
messages remain. Research itself failed quote validation; this verifies the
resource lifecycle, not content acceptance or all future browser behavior.

The broader relevant test selection passed 307 tests, including browser recovery
and route teardown; a separate browser selection passed 29 tests. Ruff, mypy,
Pyrefly, ty and the runtime resource manifest passed. No full pytest or push.

## Recurrence during the later q8 validation

`artifacts/research-q8-gap-20261031/live-01-console.log` again captured an
unobserved TargetClosedError future at shutdown after a completed real Research
run. The route-wait implementation was present and was not changed by the q8
work. The later `live-03` shutdown did not reproduce the warning, which does not
close the intermittent lifecycle issue. The earlier located race and its test
remain valid evidence, but browser teardown is not fully resolved; further
creation-stack diagnostics are needed. The q8 change does not filter or suppress
this warning. See [the q8 validation report](research-q8-gap-20261031.zh-CN.md).
