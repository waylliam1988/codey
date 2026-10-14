# Codey Test Report

This file records the current verification state, not an append-only experiment diary.

## Current working tree (2026-10-14)

| Check | Result |
| --- | --- |
| Final complete pytest | **8584 passed, 33 skipped, 1520 subtests; 969.87s** |
| Final required machine contracts | Not rerun in this change (prior baseline: **1125 passed, 14 subtests; no skips**) |
| Production statement coverage | **88.54%** (55,370 / 62,536); CI minimum **85%** |
| Production branch coverage | **78.23%** (17,332 / 22,154); CI minimum **75%** |
| Coverage scope | All **413** production Python files; Python subprocesses included |
| Full-tree mypy / Pyrefly | **417 source files, 0 errors / 0 errors** |
| Ruff / diff check | passed |
| TypeScript / Biome / Vitest | **30 tests passed; build passed** |
| Real Edge workflow regressions | **3 passed** (hybrid graph, truncated file preview, focus/reopen) |
| JavaScript syntax / wheel assets | publish check passed |
| Documentation pre-commit architecture/CI/coverage checks | passed |

This change adds regression coverage for hybrid research result projection and restore ownership, independent Git file previews after aggregate diff truncation, snapshot/Git preview separation, stale preview protection, and focus-path reopening. The proof review implementation is split into `proof_terms.py`, `proof_coverage.py`, `proof_helpers.py`, and `proof_relations.py` without changing its public contract. Real Edge verification covered the hybrid graph entry and truncated file preview workflows. The final complete run is green.

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
