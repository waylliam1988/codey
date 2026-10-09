# Pyrefly baseline (2026-10-09)

Pyrefly 1.3.2 is run alongside mypy in CI. Mypy remains the full-tree strict
gate on the existing Windows/Python matrix and Linux job. Pyrefly runs once on
Windows/Python 3.12, using the same pinned CI dependencies as the baseline
generation environment.

## Baseline policy

The baseline in [`tools/pyrefly-baseline-2026-10-09.json`](../tools/pyrefly-baseline-2026-10-09.json)
records the 235 active Pyrefly diagnostics present when it was created. CI
fails on diagnostics that are not covered by the baseline. A clean Pyrefly CI
run therefore means there are no new diagnostics; it does not mean that the
existing diagnostics have all been resolved.

Pyrefly currently reads the existing `[tool.mypy]` configuration in memory and
uses its `legacy` preset. No separate Pyrefly configuration is committed yet.
This keeps the initial rollout aligned with the existing mypy project scope
while allowing the two checkers to run together.

## Updating the baseline

Review new diagnostics before deciding whether they are valid issues, checker
or configuration differences, or items to suppress. After intentionally
accepting a set of existing diagnostics, regenerate the baseline with the
pinned Pyrefly version and the CI dependency set:

```powershell
pyrefly check codey --baseline=tools/pyrefly-baseline-2026-10-09.json --update-baseline
```

Review the JSON diff before committing it. Do not update the baseline merely to
make a failing CI run pass.
