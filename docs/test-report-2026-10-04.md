# Test Report (2026-10-04)

## Current audit result

The latest single full run returned **7187 passed, 29 skipped, 1 failed,
1497 subtests passed in 449.65s**. The lone old Ghost/router assertion expected
`done` after Reviewer failure; it was migrated to unavailable/no-success facts.
The post-full affected suite passed **44 cases in 5.06s**, with no production
changes and no repeat full run. This is not a zero-failure full-run claim.

Static checks passed; the machine gate passed **268 cases in 31.32s**. Final
KoboldCpp review smoke passed **1/1**, 1 request, no retry, **5.498s**, 756 reported
tokens. Windows privilege/platform and browser opt-in skips are recorded in
[TEST_REPORT](../TEST_REPORT.md). Node-backed script tests now execute with Node
24.19.0; the earlier missing-Node results below remain historical evidence.

See the [function ownership and fixes](review-audit-2026-10-04.zh-CN.md).

## Review hardening follow-up

- `python -m ruff check codey tests`: passed.
- `python -m mypy codey`: **0 errors**, 370 source files checked.
- `python -m compileall -q codey tests`: passed.
- Focused review/API/reuse/ledger suite: **265 passed**.
- Full `python -m pytest -q`: **7111 passed, 14 skipped, 5 failed, 1497
  subtests passed** in **439.74s**.

The five failures were all browser JavaScript tests that launch `node`:

- `tests/test_operator_bootstrap_before_ui_requests.py` (3 cases)
- `tests/test_sse_browser_dedup_reset_and_buffer_gap.py` (2 cases)

Each failed before the script ran with Windows `FileNotFoundError: [WinError 2]`
because `node` is unavailable in the environment. The tests were left as real
failures; no skip, xfail, or assertion weakening was added. The Python review
implementation and its focused regression suite passed.

## Review-specific coverage

The follow-up locks strict model identity matching, one repair turn for
structured unknown/incomplete responses, explicit reuse source propagation,
artifact hash verification, matching ledger event attempts, partial ledger
write behavior, and API rejection of reuse input on unsupported intents.
