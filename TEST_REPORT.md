# Codey Test Report

This file records the current verification state, not an append-only experiment diary.

## Current working tree (2026-10-13)

| Check | Result |
| --- | --- |
| Final complete pytest | **8574 passed, 7 skipped, 1510 subtests; 995.82s** |
| Final required machine contracts | **1125 passed, 14 subtests; no skips; 289.44s** |
| Production statement coverage | **88.54%** (55,370 / 62,536); CI minimum **85%** |
| Production branch coverage | **78.23%** (17,332 / 22,154); CI minimum **75%** |
| Coverage scope | All **413** production Python files; Python subprocesses included |
| Full-tree mypy / Pyrefly | **413 source files, 0 errors / 0 errors** |
| Ruff / JavaScript syntax / wheel assets / diff check | passed |
| Documentation pre-commit architecture/CI/coverage checks | **117 passed, 383 subtests** |

The first measured suite had two outdated UI tests (an old Failed label assertion and a Node window stub missing resize listeners). Only tests were repaired; final complete pytest and machine contracts above are green. The coverage data measures the same production source. Coverage HTML/JSON/XML and full logs remain under ignored `.e2e-artifacts/` and are uploaded by CI.

Live Zen matrix: **36/42** initial cases passed; four of six failed cases passed on retest. Two Ling thinking-enabled cases remain unresolved. The free-model matrix is not all green. Local validation does not claim a new GitHub CI run or a release.

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
