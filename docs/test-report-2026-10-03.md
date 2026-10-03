# Test Report (2026-10-03)

## Explicit TaskSubmissionStores

- Added `TaskSubmissionStores`, `AppContext.build_task_submission_stores()`,
  and `TaskRunDeps.from_submission_stores()`.
- HTTP and headless submission now use the same typed assembly path; the
  `TaskSubmissionState` protocol exposes one resource bundle instead of nine
  store properties.
- Full-tree mypy: **0 errors**, 363 source files checked; ruff and diff checks
  passed before pytest.
- Full command: `python -m pytest -q -o faulthandler_timeout=120`
- Full result: **6663 passed, 12 skipped, 1 failed, 1488 subtests passed** in
  **419.89s**. The single failure was a stale architecture assertion for the
  old constructor spelling. It was corrected and the affected tests passed in
  targeted recheck; the full suite was not repeated.

## Final typing boundary pass

The staged cleanup now covers 16 incremental mypy modules, including the
task loop, structured verification commands, evidence ledger, trace, and
platform process boundary.

- `python -m mypy codey`: **0 errors**, 363 source files checked.
- `python -m mypy --follow-imports=skip --ignore-missing-imports` on the 16
  gated modules: passed.
- `python -m ruff check codey tests tools`: passed.
- `git diff --check`: passed.
- Targeted regression recheck after the full run: **10 passed, 10 subtests**.
- The single full run was `python -m pytest -q -o faulthandler_timeout=120`:
  **6651 passed, 12 skipped, 9 failed, 1480 subtests passed** in
  **459.58s**. The nine failures were deterministic regressions identified
  and fixed afterward; the affected tests all passed in the targeted recheck.
  Per the execution constraint, the full suite was not repeated.

## Scope

Typed application, payload, and process-tree boundaries were updated after
red-first mypy tests reproduced the original diagnostics. The CI incremental
gate now covers 16 modules.

## Verification

- `python -m mypy --follow-imports=skip --ignore-missing-imports` on the 16
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
- Current full-tree run: 0 errors in 363 source files.
- The 16-module incremental boundary remains enforced in CI.
