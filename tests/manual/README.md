# Manual live benchmarks

`project_map_symbol_ab.py` compares first-file selection with only the initial
listing, with the current Project Map, and with Project Map plus the
task-aware Symbol overview:

```powershell
python -B tests\manual\project_map_symbol_ab.py `
  --provider deepseek `
  --port 9222 `
  --out .repo-map-probe.deepseek.strict.json
```

It asks the live provider to return only the files it would inspect first. Use
it to validate navigation changes, not as a product tool. `--self-test` checks
the local Symbol overview invariants without opening a provider page.

`qwen_submit_probe.py` is a narrow live diagnostic for Qwen submit/readiness
issues:

```powershell
python -B tests\manual\qwen_submit_probe.py --timeout 45
```

It can reuse the Project Map/Symbol overview benchmark prompts with `--case`
and `--arm` when diagnosing Qwen-specific stalls.

`read_before_edit_ab.py` compares live task completion with the read-before-edit
guard disabled and enabled:

```powershell
python -B tests\manual\read_before_edit_ab.py `
  --provider deepseek `
  --case similar-config-constant `
  --arm guard `
  --port 9222 `
  --out .read-before-edit.deepseek.json
```

It uses temporary projects and records guard blocks, whether the model reads
after a block, final local test success, turns, tool calls, and changed files.
Omit `--arm` for the full baseline/guard A/B; select one arm for a lightweight
provider smoke.

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

`context_delta_ab.py` tests whether Codey's short same-web-conversation
follow-up has measurable value compared with repeating the complete project
intro. Both arms first run the same read-only orientation task. The second
stage either sends the full context again, sends only the new request, or uses
`contract-delta` to repeat the stable tool contract without repeating Project
Map, project instructions, or the initial listing:

```powershell
python -B tests\manual\context_delta_ab.py `
  --case codey_execution_evidence `
  --arm delta `
  --provider deepseek `
  --port 9222
```

Run `full`, `delta`, and `contract-delta` in separate fresh benchmark
conversations. Compare
follow-up correctness first, then `followup_turns`,
`repeated_warmup_information_calls`, `followup_first_prompt_chars`, total sent
characters, and provider time. The harness disables edit and run, never
executes Shell, and writes results to the system temporary directory.
If the first-stage orientation does not complete cleanly, the sample is marked
`eligible=false` and the follow-up is not run.

`verification_review_ab.py` compares the same synthetic diff with and without
the hidden Verification Map:

```powershell
python -B tests\manual\verification_review_ab.py `
  --provider deepseek `
  --arm current `
  --port 9222
```

It is review-only and never opens or changes a local project.
