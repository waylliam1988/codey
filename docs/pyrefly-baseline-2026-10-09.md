# Pyrefly full-tree gate (2026-10-09)

Pyrefly 1.3.2 runs alongside mypy in CI. Mypy remains the strict full-tree
gate on Windows/Python 3.11–3.13 and Linux/Python 3.12. Pyrefly runs once on
Windows/Python 3.12 with the pinned CI dependencies.

The original rollout baseline contained 235 diagnostics. Before cleanup,
the current tree had 236: the additional diagnostic was an implicit `Any`
return in `ExecutionDelegate.handles`. All 236 active errors have now been
resolved. The old baseline file has been reduced to an empty `errors` array;
CI runs `pyrefly check codey` directly, without baseline exemptions.

Pyrefly still imports the existing `[tool.mypy]` configuration using its
`legacy` preset. No checker settings were relaxed and no new suppression
comments were added. The final check reports 0 errors, 47 suppressed diagnostics
and 838 warnings not shown. Suppressed diagnostics and warnings remain separate
from the active-error gate.

Run the same gates locally:

```powershell
pyrefly check codey
python -m mypy codey
python -m ruff check .
```

The final Windows/Python 3.12 suite passed 8098 tests, with 7 skips and 1503
passing subtests. See [the existing test record](../TEST_REPORT.md) for validation.

Fix new diagnostics in source. Do not add baseline exemptions merely to make
CI pass. The empty baseline is retained as rollout history, not used by CI.
