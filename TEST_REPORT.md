# Codey Test Report

This file records the current verification state, not an append-only experiment diary.

## Current working tree (2026-10-15)

| Check | Result |
| --- | --- |
| Final complete pytest | **8711 passed, 8 skipped, 1519 subtests; 1100.02s** |
| Final required machine contracts | **1129 passed, 14 subtests; no skips; 302.57s** |
| Production statement coverage | **88.51%** (56,091 / 63,374); CI minimum **85%** |
| Production branch coverage | **78.17%** (17,614 / 22,534); CI minimum **75%** |
| Coverage scope | All **419** production Python files; Python subprocesses included |
| Full-tree mypy / Pyrefly / ty | **419 source files; all three checkers passed for Python 3.11, 3.12 and 3.13 targets** |
| Ruff / diff check | passed |
| TypeScript / Biome / Vitest | **66 tests passed; build passed** |
| Real HTTP protocol continuation | **22 passed**, using deterministic local peers for both native protocols |
| Real HTTP Stop stress | **100 passed**: 50 before headers, 50 during silent error bodies |
| Transport regression group | **64 passed**; final implementation also passed under coverage |
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
Real HTTP/browser checks cover approval restart, Allow/Deny, continuation retry,
workspace document preview and sidebar actions. The native socket fix retains
the one-second Stop assertion and preserves unrelated I/O errors.

Coverage comes from the complete-suite capture plus focused reruns after fixes.
Measurements for the two subsequently changed production modules were purged
before recapturing their final code (58 approval tests and 64 transport tests),
then combined with unchanged-module data. The full-production coverage gate and
JSON/XML/HTML generation passed. A separate final complete pytest run validates
the final implementation together.

The eight skips are explicit environment gates: six require POSIX permission,
process-group, absolute-path or `O_NOFOLLOW` behavior; two opt into an external
workspace smoke or the separate real desktop-browser workflow. The required
machine-contract gate has no skips. The actual HTTP/Chromium approval and
document regressions run normally in the complete suite.

Logs and installed-package UI evidence are under ignored
`.e2e-artifacts/review-20261015/`; final full-suite and machine-contract logs are
`full-pytest-release.log` and `machine-contracts-release.log`. Coverage reports are
under its `complete/` directory.

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
