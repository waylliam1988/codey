# Test Report (2026-10-03)

## Scope

Typed application, payload, and process-tree boundaries were updated after
red-first mypy tests reproduced the original diagnostics. The CI incremental
gate now covers 11 modules.

## Verification

- `python -m mypy --follow-imports=skip --ignore-missing-imports` on the 11
  gated modules: passed.
- `python -m ruff check codey tests tools`: passed.
- `git diff --check`: passed.
- `python -m compileall -q codey`: passed.
- Targeted boundary and behavior tests: 140 passed, 2 skipped, 14 subtests.
- Full command: `python -m pytest -q -o faulthandler_timeout=120`
- Full result: **6633 passed, 34 skipped, 1488 subtests passed** in
  **413.81s**. No failed, xfailed, or xpassed tests.

## Typing Progress

- Frozen baseline: 506 errors in 138 files, 363 source files checked.
- Current full-tree run: 354 errors in 114 files, 363 source files checked.
- The full tree remains a non-gating migration baseline; the 11-module
  boundary gate is the enforced CI contract.
