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
