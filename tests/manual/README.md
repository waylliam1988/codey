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

`scoped_task_plan_ab.py` compares current task-aware Project Map navigation
with a deterministic local scope hint. The default `current,hint` arms ask for
first files directly; `hint` adds a local advisory "Deterministic Scope Hint"
computed from the current project map scoring signals, without an extra model
planning turn. The older `scoped` arm can still be selected explicitly to
retest the two-step hidden planner:

```powershell
python -B tests\manual\scoped_task_plan_ab.py `
  --provider all `
  --port 9222 `
  --case stockalarm-training-flow `
  --case stockalarm-backtest-masks `
  --arms current,hint
```

It records path, test-path, and expected-term hits plus sent characters and
provider time. The probe never edits files, never runs commands, and writes its
JSON report to the system temporary directory by default.

`zoom_project_map_ab.py` tests the production Focused subtree section against
the legacy task-aware Project Map shape. It creates a temporary deep synthetic
monorepo with most target filenames omitted from the task text, then compares:

- `current`: the legacy task-aware map shape, with the ordinary Symbol overview
  and without Focused subtree.
- `zoom`: production `render_project_map(..., task=...)`, including the
  bounded `Focused subtree` section.

```powershell
python -B tests\manual\zoom_project_map_ab.py `
  --provider all `
  --max-cases 4 `
  --output .zoom-project-map-ab.live.json
```

Use `summary.unnamed_deep` for the decision. The original probe required
better `top1_path_hits` without larger prompts; after production integration,
the compatibility check is whether the `Focused subtree` section recovers deep
targets while the full Project Map remains under the bounded map character
budget.

`task_lens_ab.py` is a probe-only benchmark for a possible Coverage-aware Task
Lens. It compares the current production `render_project_map(..., task=task)`
against a prototype that replaces Focused subtree / Symbol overview with a
short `Task Lens` block. It supports a cheap first-file selection mode and a
read-only agent navigation mode:

```powershell
python -B tests\manual\task_lens_ab.py `
  --provider qwen `
  --mode pick `
  --max-cases 4 `
  --output .task-lens-ab.pick.qwen.json

python -B tests\manual\task_lens_ab.py `
  --provider qwen `
  --mode readonly `
  --max-cases 2 `
  --output .task-lens-ab.readonly.qwen.json
```

Use this before changing production Project Map output. The `current` arm must
remain the current production Project Map, not the older legacy map from
`zoom_project_map_ab.py`.

2026-07-16 live result: do not ship this Task Lens prototype yet. In file-pick
mode across DeepSeek, MiMo, Qwen, and GLM, both arms were already saturated:
`current` and `lens` each reached 16/16 top1 path hits, 32 path hits, and 16
test hits. The lens arm reduced prompt text by only 560 total characters. In
read-only mode across Qwen, MiMo, and GLM on two unnamed deep cases each,
`current` completed 6/6 correctly with 18 tool calls, while `lens` completed
5/6 correctly with 24 tool calls and one extra search. DeepSeek's read-only run
hit a provider `rate_limited` send failure and was excluded from the paired
readonly aggregate. The result supports keeping this as a regression probe
instead of changing production Project Map output.

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

`edit_failure_context_ab.py` simulates a file changing after the model reads it,
then compares the existing generic replacement error with the same error plus a
bounded current-file excerpt. It is a probe only and does not change production
edit behavior:

```powershell
python -B tests\manual\edit_failure_context_ab.py --provider mimo --port 9222
```

`default_verification_ab.py` compares the pre-0.1.35 completion behavior with
the bounded production policy for trusted post-edit verification. Run one
case/arm at a time to avoid provider rate limits:

```powershell
python -B tests\manual\default_verification_ab.py `
  --provider mimo `
  --case python-pytest `
  --arm current
```

`provider_revival_smoke.py` performs a live, temporary-store fault injection
for Provider Revival. It invalidates the selected provider's composer
selectors in memory, requires a sibling model to select the send button, then
checks that a second natural send reuses and promotes the recovered bundle:

```powershell
python -B tests\manual\provider_revival_smoke.py --provider all --port 9222
```

`adapter_self_repair_smoke.py` performs a live smoke for the Provider adapter
self-repair path. It does not install an override. Instead it checks that the
background repair helper can use a fresh tab in the logged-in Codey browser
profile, and that a candidate Provider worker can run a neutral marker canary:

```powershell
python -B tests\manual\adapter_self_repair_smoke.py --provider qwen --timeout 90
python -B tests\manual\adapter_self_repair_smoke.py --provider deepseek --timeout 90
python -B tests\manual\adapter_self_repair_smoke.py --provider mimo --timeout 90
python -B tests\manual\adapter_self_repair_smoke.py --provider glm --timeout 90
```

Reports contain only bounded status metadata, marker length/exactness, timing,
and error type/message snippets. They do not store prompts, replies, cookies,
page text, DOM, or project data.

`mimo_typing_evidence_probe.py` records only bounded boolean DOM evidence for
MiMo's `data-is-typing` transition. It never writes the prompt or reply text to
the report, and its outbound nonce uses the same product-neutral
`SESSION_CHECK_<random>` form as the other web probes. Run short, long-code,
and deep-thinking cases separately before
retaining MiMo completion Flow; `--force-flow` disables only the built-in
completion decision for the first send so the production Flow path must save a
provisional rule, then performs a second natural send in a fresh task context
and requires promotion to active:

```powershell
python -B tests\manual\mimo_typing_evidence_probe.py --case short
python -B tests\manual\mimo_typing_evidence_probe.py --case long-code
python -B tests\manual\mimo_typing_evidence_probe.py --case deep-thinking
python -B tests\manual\mimo_typing_evidence_probe.py --case short --force-flow
```

`python_syntax_regression_ab.py` compares the production Python
syntax-regression hint with a baseline that suppresses it. Fault injection is
probe-only: it inserts the same missing colon in both A/B arms and runs a
separate valid-edit control. Production parsing is skipped above 128K
characters; this is a character budget, not a byte-size limit:

```powershell
python -B tests\manual\python_syntax_regression_ab.py --provider deepseek
python -B tests\manual\python_syntax_regression_ab.py --provider qwen
python -B tests\manual\python_syntax_regression_ab.py --provider mimo
python -B tests\manual\python_syntax_regression_ab.py --provider glm
```

The default order is baseline-first for DeepSeek/Qwen and hint-first for
MiMo/GLM. Use `--order baseline-first` or `--order hint-first` to override it.

`refactor_hint_ab.py` compares current production edits with a probe-only
incomplete-refactor hint. The hint arm monkeypatches successful replacement
edits only inside the script: after a narrow identifier rename, it runs a
bounded lexical scan for the old symbol in other Python/JS/TS source files and
adds only a file-count note, with no source excerpts:

```powershell
python -B tests\manual\refactor_hint_ab.py `
  --provider deepseek `
  --case python-function-rename
```

Available cases are `python-function-rename`, `python-class-rename`,
`implicit-function-rename`, and `public-string-control`. The implicit case uses
a shorter user-style rename request; the control case checks whether the hint
causes a model to over-rename an external string contract.

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

`scan_coverage_ab.py` compares the old low-level `find_reference_hints` output
with Writer's production coverage rendering when the bounded reference scan
skips oversized files:

```powershell
python -B tests\manual\scan_coverage_ab.py `
  --provider deepseek `
  --case references-oversized-omission
```

Use `--provider all` to run the same baseline/coverage arms across DeepSeek,
MiMo, Qwen, and GLM. The fixture is temporary, read-only, and the coverage arm
does not expose skipped file contents; it only reports skipped path examples
and omission reasons. The baseline arm reconstructs the old low-level
`find_reference_hints` output without Writer coverage rendering, so the probe
remains a real A/B after production `find_references` starts exposing scan
coverage.

The scorer records `protocol_success` separately from `semantic_safe` so a
provider can be credited for the right conclusion even when its JSON tool call
is malformed. It also flags two coverage-specific failure modes:
`contradictory_absence_with_reference` and `false_scan_complete_claim`.

2026-07-16 live rerun, one provider at a time against Edge CDP on port 9222:

- DeepSeek: coverage stayed semantically safe and reduced the run from 3 turns,
  2 tools, and 1 follow-up search to 2 turns, 1 tool, and no search.
- MiMo: both arms were semantically safe; coverage reduced the run from 4 turns
  and 3 tools to 3 turns and 2 tools.
- Qwen: coverage stayed semantically safe and reduced the run from 3 turns,
  2 tools, and 1 search to 2 turns, 1 tool, and no search.
- GLM: baseline was unsafe (`bad_confident_absence=true` and
  `false_scan_complete_claim=true`); coverage made the incomplete scan explicit
  and produced a semantically safe answer.

The result supports a narrow `ScanReport` slice for `find_references`: the
main value is reducing false certainty when bounded scans skip files, not raw
speed. The probe remains useful as a regression check for the production Writer
coverage output.
