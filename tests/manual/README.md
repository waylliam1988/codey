# Manual live benchmarks

`large_project_ab.py` measures Codey's read-only navigation behavior against
real medium/large projects through an already-open web provider.

Run one arm at a time so provider throttling does not contaminate the result:

```powershell
python -B tests\manual\large_project_ab.py `
  --case stockalarm_backtest_masks `
  --arm current `
  --provider glm `
  --port 9222
```

Available arms:

- `baseline`: basic list/read/literal grep, without Project Map or references.
- `current`: the current Codey read-only navigation stack.

The harness replaces edit, write, and run entry points with read-only errors.
It writes results to the system temporary directory by default, not either
tested repository. Use `--output` to choose another path outside the projects.

Compare correctness first, then tool turns, sent characters, and provider time.
Single-run wall-clock differences are noisy because web providers throttle and
vary their generation latency.

`verification_review_ab.py` compares the same synthetic diff with and without
the hidden Verification Map:

```powershell
python -B tests\manual\verification_review_ab.py `
  --provider deepseek `
  --arm current `
  --port 9222
```

It is review-only and never opens or changes a local project.
