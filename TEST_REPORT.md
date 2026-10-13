# Codey Test Report

This file records the current verification state, not an append-only experiment diary.

## Current working tree (2026-10-13)

| Check | Result |
| --- | --- |
| Final complete pytest | **8588 passed, 7 skipped, 1510 subtests; 990.95s** |
| Final required machine contracts | Not rerun in this change (prior baseline: **1125 passed, 14 subtests; no skips**) |
| Production statement coverage | **88.54%** (55,370 / 62,536); CI minimum **85%** |
| Production branch coverage | **78.23%** (17,332 / 22,154); CI minimum **75%** |
| Coverage scope | All **413** production Python files; Python subprocesses included |
| Full-tree mypy / Pyrefly | **413 source files, 0 errors / 0 errors** |
| Ruff / diff check | passed |
| JavaScript syntax / wheel assets | Not rerun in this change |
| Documentation pre-commit architecture/CI/coverage checks | **117 passed, 383 subtests** |

This change adds regression coverage for atomic approval verdicts, abandoned approvals, complete cold-start command fields, source-run delivery ownership, delivery-witness reconciliation, Zen probe settlement/waiting, credential-scoped proxy pools, and the current AgentRequest live-smoke harness. A complete run exposed legacy RequestPolicy test doubles and optional ledger mocks; both were corrected and the final complete run is green. Headed browser verification completed the release flow checks for provider selection, project read/write/test, receipts, diff/restore, shell approval denial/reconnect, SSE reconciliation, and responsive stop. Real Zen runs also completed discussion, file creation plus tests, and reference search/read/edit/test. The coverage and machine-contract figures shown above are prior measured baselines; they were not rerun in this change.

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
