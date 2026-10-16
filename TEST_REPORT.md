# Codey Test Report

This file records the current verification state, not an append-only experiment diary.

## Current verification (2026-10-17)

| Check | Result |
| --- | --- |
| Final complete pytest | **8784 passed, 8 skipped, 1520 subtests; 1531.36s** |
| Final required machine contracts | **1129 passed, 14 subtests; no skips; 337.24s** |
| Production statement coverage | **88.53%**; CI minimum **85%** |
| Production branch coverage | **78.24%**; CI minimum **75%** |
| Coverage scope | All **429** production Python files; Python subprocesses included |
| Full-tree mypy / Pyrefly / ty | **431 source files; all three checkers passed for Python 3.12 target** |
| Ruff / diff check | passed |
| TypeScript / Biome / Vitest | **34 files / 132 tests passed; typecheck, build and published metadata check passed** |
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
generation passed. The new approval tests are included in the final suite.
The coverage percentages above remain the latest collected coverage snapshot;
the final pytest rerun recorded here was not used to replace them without a new
coverage collection.

This change also verified a clean Python 3.12 environment from the generated
`requirements-ci.txt`: hash-checked installation, editable project installation
with `--no-deps --no-build-isolation`, and `pip check` all passed. CI contract
tests confirm both Windows and Linux jobs consume that lock without
reresolving. The EOF stability cases (including slow startup), watchdog timeout,
approval HTTP restart, browser approval, and continuation lifecycle regressions
all passed. The migrated Allow/Deny response reconciliation and conversation UI
regressions run in Vitest against the real frontend modules; the Python bridge
tests and enum shims were removed.

The eight skips are explicit environment gates: six require POSIX permission,
process-group, absolute-path or `O_NOFOLLOW` behavior; two opt into an external
workspace smoke or the separate real desktop-browser workflow. The required
machine-contract gate has no skips. The actual HTTP/Chromium approval and
document regressions run normally in the complete suite.

Installed-package UI evidence is under ignored
`.e2e-artifacts/review-20261015/`. Coverage reports are under
`.e2e-artifacts/coverage/`.

Live Zen matrix: **36/42** initial cases passed; four of six failed cases passed on retest. Two Ling thinking-enabled cases remain unresolved. The free-model matrix is not all green. This review used local deterministic peers rather than new live-provider calls; its results do not establish live-provider health or release status.

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
