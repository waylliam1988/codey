# Bounded Research Planner A/B Reports

Generated from live manual results on 2026-08-20.

This report records the 0.4.4 bounded planner experiments that were run while
keeping production Research code unchanged. The harness changes live in
`tests/manual/bounded_research_planner_ab.py`; result JSON and trace files were
written under `tests/manual/results/`.

## Decision

Do not enable a production planner behavior from these results alone.

The live runs show that a planner can add value when three conditions are true:

1. The initial Research turn is kept to already visible material.
2. The planner executor can fetch genuinely new material.
3. The final result is merged as a narrow evidence patch, not as a full report
   rewrite.

The strongest current signal is the evidence-only patch-merge probe. StepFun and
GLM both moved `widget_noop` from score 1 to 6 with one follow-up round, one new
source, one new evidence item, and no unsupported-claim regression. Earlier runs
showed that prompt-only follow-up is unstable: models may rewrite too much, cite
synthetic URLs too broadly, wrap JSON in code fences, or fail to persist new
evidence before `done`.

## Result Files

- `bounded_research_planner_ab-deepseek-20260820.json`
- `bounded_research_planner_ab-deepseek-syntheticurls-paired-widget-20260820.json`
- `bounded_research_planner_ab-mimo-20260820.json`
- `bounded_research_planner_ab-mimo-nowall-20260820.json`
- `bounded_research_planner_ab-mimo-freshmaterial-20260820.json`
- `bounded_research_planner_ab-mimo-syntheticurls-paired-widget-20260820.json`
- `bounded_research_planner_ab-mimo-patchmerge-paired-widget-20260820.json`
- `bounded_research_planner_ab-mimo-materialpatch-paired-widget-20260820.json`
- `bounded_research_planner_ab-mimo-hiddenmaterial-paired-widget-20260820.json`
- `bounded_research_planner_ab-mimo-hiddenmaterial2-paired-widget-20260820.json`
- `bounded_research_planner_ab-deepseek-hiddenmaterial-paired-widget-20260820.json`
- `bounded_research_planner_ab-qwen-20260820.json`
- `bounded_research_planner_ab-qwen-hiddenmaterial2-paired-widget-20260820.json`
- `bounded_research_planner_ab-glm-hiddenmaterial-paired-widget-20260820.json`
- `bounded_research_planner_ab-stepfun-hiddenmaterial-paired-widget-20260820.json`
- `bounded_research_planner_ab-stepfun-evidenceonly3-paired-widget-20260820.json`
- `bounded_research_planner_ab-glm-evidenceonly3-paired-widget-20260820.json`

## Experiment Timeline

### Initial DeepSeek Run

File: `bounded_research_planner_ab-deepseek-20260820.json`

| case | baseline | planner | delta | follow-up | stop | useful |
|---|---:|---:|---:|---:|---|---|
| `warehouse_gap` | 4 | 8 | +4 | 0 | `no_actionable_gap` | false |
| `widget_noop` | 5 | 5 | 0 | 1 | `max_followup_rounds` | false |

Finding: the score improvement on `warehouse_gap` was not caused by follow-up;
the planner did not run. `widget_noop` ran one follow-up round but gained no new
source or evidence. This did not justify production changes.

### Initial MiMo Run

File: `bounded_research_planner_ab-mimo-20260820.json`

| case | baseline | planner | delta | follow-up | stop | useful |
|---|---:|---:|---:|---:|---|---|
| `warehouse_gap` | 4 | 6 | +2 | 0 | `max_wall_time` | false |
| `widget_noop` | 5 | 5 | 0 | 0 | `max_wall_time` | false |

Finding: `max_wall_time` masked planner behavior. The run supported removing
wall time as a success gate in the A/B harness and treating time as a diagnostic
cost instead.

### MiMo No-Wall Run

File: `bounded_research_planner_ab-mimo-nowall-20260820.json`

| case | baseline | planner | delta | follow-up | stop | useful |
|---|---:|---:|---:|---:|---|---|
| `warehouse_gap` | 6 | 8 | +2 | 0 | `no_actionable_gap` | false |
| `widget_noop` | 5 | 5 | 0 | 1 | `max_followup_rounds` | false |

Finding: removing wall time exposed that the core issue was not just timeout.
The planner still failed to add useful material in the follow-up path.

### Fresh-Material Probe

File: `bounded_research_planner_ab-mimo-freshmaterial-20260820.json`

| case | baseline | planner | delta | follow-up | material gain | execution material | useful |
|---|---:|---:|---:|---:|---|---|---|
| `warehouse_gap` | 4 | 4 | 0 | 0 | false | false | false |
| `widget_noop` | 5 | 5 | 0 | 1 | false | true | false |

Finding: the executor could fetch a fresh URL for `widget_noop`, but the final
`ResearchRecord` did not gain a source or evidence. This isolated the bottleneck
to follow-up synthesis absorption, not search/fetch.

### Synthetic URL Paired Runs

Files:

- `bounded_research_planner_ab-mimo-syntheticurls-paired-widget-20260820.json`
- `bounded_research_planner_ab-deepseek-syntheticurls-paired-widget-20260820.json`

| provider | baseline | planner | delta | follow-up | material gain | coverage delta | unsupported delta | useful |
|---|---:|---:|---:|---:|---|---:|---:|---|
| MiMo | 5 | 6 | +1 | 1 | true | +0.111 | -0.500 | true |
| DeepSeek | 5 | 6 | +1 | 1 | true | -0.111 | 0.000 | false |

Finding: synthetic `.test` URLs removed the earlier provider behavior where a
model tried to open external fake links directly. MiMo showed a real gain, but
DeepSeek showed that full-report follow-up can still lower coverage even when
new material is absorbed.

### Prompt-Only Patch Merge Attempt

File: `bounded_research_planner_ab-mimo-patchmerge-paired-widget-20260820.json`

| baseline | planner | delta | follow-up | execution material | patch reason | useful |
|---:|---:|---:|---:|---|---|---|
| 5 | 5 | 0 | 1 | true | `no_new_patch_evidence` | false |

Finding: MiMo fetched `source-b`, but repeated `knowledge_write` attempts were
wrapped in fenced JSON or used controller IDs instead of canonical final URLs.
The follow-up did not persist new evidence, so there was nothing safe to merge.

### Material Patch Without Hidden Material

File: `bounded_research_planner_ab-mimo-materialpatch-paired-widget-20260820.json`

| baseline | planner | delta | follow-up | stop | material gain | useful |
|---:|---:|---:|---:|---|---|---|
| 5 | 6 | +1 | 0 | `no_new_material` | true | false |

Finding: this was not a valid proof of patch-only follow-up. The initial
planner arm saw and wrote `source-b` before follow-up because the fixture leaked
material based on broad query terms like `current`.

### Hidden-Material Patch-Only Runs

Files:

- `bounded_research_planner_ab-mimo-hiddenmaterial-paired-widget-20260820.json`
- `bounded_research_planner_ab-mimo-hiddenmaterial2-paired-widget-20260820.json`
- `bounded_research_planner_ab-deepseek-hiddenmaterial-paired-widget-20260820.json`
- `bounded_research_planner_ab-qwen-hiddenmaterial2-paired-widget-20260820.json`
- `bounded_research_planner_ab-glm-hiddenmaterial-paired-widget-20260820.json`
- `bounded_research_planner_ab-stepfun-hiddenmaterial-paired-widget-20260820.json`

The harness was changed so the normal Research model search only sees default
source A. Only the A/B `PlanExecutor` material phase can retrieve hidden source
B. This creates the intended test condition: initial answer from A, planner
execution opens B, follow-up/patch path integrates B.

| file | baseline | planner | delta | follow-up | patch | sources | evidence | coverage delta | unsupported delta | useful |
|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---|
| `hiddenmaterial` | 5 | 6 | +1 | 1 | `patch_only_merge` | 1 -> 2 | 1 -> 2 | 0.000 | -0.125 | true |
| `hiddenmaterial2` | 5 | 6 | +1 | 1 | `patch_only_merge` | 1 -> 2 | 1 -> 2 | +0.111 | -0.300 | true |
| `deepseek-hiddenmaterial` | 5 | 6 | +1 | 1 | `patch_only_merge` | 1 -> 2 | 1 -> 2 | +0.111 | -0.083 | true |
| `qwen-hiddenmaterial2` | 5 | 6 | +1 | 1 | `patch_only_merge` | 1 -> 2 | 1 -> 2 | +0.111 | -0.083 | true |
| `glm-hiddenmaterial` | 1 | 6 | +5 | 1 | `patch_only_merge` | 1 -> 2 | 1 -> 2 | +0.223 | +0.400 | false |
| `stepfun-hiddenmaterial` | 1 | 1 | 0 | 0 | `no_new_patch_source` | 1 -> 1 | 1 -> 1 | 0.000 | 0.000 | false |

Finding: this is the first clean evidence that the desired design can help. The
MiMo `hiddenmaterial2` run improved coverage from 0.556 to 0.667 and
unsupported claim rate from 0.800 to 0.500 with one follow-up round and one new
evidence-backed source. DeepSeek and Qwen both replicated the same +1 score and
+0.111 coverage lift while reducing unsupported claim rate by 0.083.

Cost for `hiddenmaterial2`: provider sends increased from 7 to 9 and elapsed
time increased by 54.066 seconds.

Cost for `deepseek-hiddenmaterial`: provider sends stayed at 7 and elapsed time
increased by 3.568 seconds.

Cost for `qwen-hiddenmaterial2`: provider sends increased from 5 to 7 and
elapsed time increased by 27.528 seconds.

GLM shows why the conservative usefulness gate is necessary. Its raw score rose
from 1 to 6 and it added the hidden source/evidence, but unsupported-claim rate
regressed from 0.000 to 0.400. The result is not safe to count as useful without
a stricter patch-only merge that prevents new unsupported conclusions.

StepFun did not reach planner execution. Both arms ended with score 1 and
`answer_status=not_answered`; the planner row stopped at
`initial_stop_reason_protocol`, so there was no material gain to evaluate.

### Evidence-Only Patch Merge Runs

Files:

- `bounded_research_planner_ab-stepfun-evidenceonly3-paired-widget-20260820.json`
- `bounded_research_planner_ab-glm-evidenceonly3-paired-widget-20260820.json`

The harness was narrowed again:

- The follow-up model can only call `knowledge_write`.
- `done`, report rewrite, search/open/read/link, and multi-tool replies are
  forbidden in the follow-up controller.
- The follow-up turn is limited to one evidence-capture turn.
- The final result is rebuilt as a deterministic evidence-only patch. It keeps
  evidence-backed base claims, creates claims for written evidence that lacked a
  claim projection, adds only fresh evidence-backed patch claims, and drops
  unsupported claims from the experimental merged record.

| provider | baseline | planner | delta | useful | follow-up | sources | evidence | claims | coverage delta | unsupported delta | send delta | time delta |
|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| StepFun | 1 | 6 | +5 | true | 1 | 1 -> 2 | 1 -> 2 | 0 -> 2 | +0.112 | 0.000 | +1 | -4.123s |
| GLM | 1 | 6 | +5 | true | 1 | 1 -> 2 | 1 -> 2 | 0 -> 2 | +0.112 | 0.000 | +1 | +40.971s |

Finding: the StepFun failure was not primarily a search/planner problem. It was
the normal follow-up/report path asking a provider with fragile long JSON
`done.answer` behavior to keep rewriting the final report. The evidence-only
probe avoids that failure mode by never asking the follow-up model to produce
`done`. GLM's earlier overclaim regression was also blocked by deterministic
merge: unsupported provider-written claims are not carried into the experimental
final record.

### Qwen

File: `bounded_research_planner_ab-qwen-20260820.json`

All rows failed with transient Qwen Studio send/new-chat errors, so Qwen was
excluded from usefulness conclusions. Follow-up diagnosis found this was not a
planner failure: Qwen's homepage exposed `textarea.message-input-textarea` and
`button.send-button` before the homepage submit handler was ready. Immediate
submit after `new_chat()` could clear the composer and leave the URL at
`https://chat.qwen.ai/` with no response. The Qwen driver now waits out that
homepage false-ready state before the first submit, and the clean hidden-material
paired run above supersedes the failed Qwen rows for planner usefulness.

## Current Harness Behavior

The A/B harness now tests these concepts without changing production code:

- Fresh-material execution skips already-opened URLs.
- Normal fixture search returns only default material.
- PlanExecutor fixture phase can reveal hidden new material.
- Follow-up material is recorded per run.
- Planner follow-up can run in the experimental evidence-only mode where the
  controller permits only `knowledge_write` and forbids `done`.
- Patch-only merge first prefers evidence actually written by the model, then
  compiles the final report as a deterministic evidence-only patch.
- If the model fails to write new evidence, an A/B-only material patch path can
  construct a deterministic source/evidence/claim patch from opened material.
- Usefulness requires both rows to be `ok`, follow-up to run, material gain,
  quality-side improvement, and no quality regression.

## Interpretation

The planner did not originally help because the design had three conflated
problems:

1. Search/fetch could fail to find genuinely new material.
2. Follow-up synthesis could see new material but fail to save it as citable
   evidence.
3. Even when evidence existed, asking the model to rewrite the full report could
   add unsupported claims, lower coverage, or fail provider JSON transport.

The hidden-material runs separate those concerns. They show the most promising
production shape is not "planner asks model to rewrite the answer"; it is:

```text
initial ResearchRecord
  + new opened material
  + evidence-only knowledge_write
  + deterministic evidence-backed patch
  -> deterministic merged ResearchRecord
```

## Web Provider Summary

| provider | baseline | planner | delta | useful | follow-up | material gain | coverage delta | unsupported delta | send delta | time delta |
|---|---:|---:|---:|---|---:|---|---:|---:|---:|---:|
| DeepSeek | 5 | 6 | +1 | true | 1 | true | +0.111 | -0.083 | 0 | +3.568s |
| MiMo | 5 | 6 | +1 | true | 1 | true | +0.111 | -0.300 | +2 | +54.066s |
| Qwen | 5 | 6 | +1 | true | 1 | true | +0.111 | -0.083 | +2 | +27.528s |
| GLM evidence-only3 | 1 | 6 | +5 | true | 1 | true | +0.112 | 0.000 | +1 | +40.971s |
| StepFun evidence-only3 | 1 | 6 | +5 | true | 1 | true | +0.112 | 0.000 | +1 | -4.123s |

Current signal: the design is directionally useful on all five tested web
providers only after the follow-up role is narrowed to evidence capture and the
final result is merged deterministically. It is still not ready to enable as a
generic production behavior from synthetic fixture evidence alone; the next
gate is a real connector-backed case plus production-design review.

## Production Criteria Before Merge

Before moving this from A/B harness into production code, run at least:

- MiMo, DeepSeek, and Qwen hidden-material `warehouse_gap` after the fixture is tuned so
  the hidden material represents a true missing limitation.
- One real connector-backed case where the second source is not synthetic.

Production code should not accept a planner result merely because it fetched a
new URL. It should require:

- new citable evidence,
- no quality regression,
- bounded deterministic merge,
- trace fields that distinguish `execution_material_gain` from final-record
  `material_gain`.

## Recommended 0.4.4 Direction

Keep the roadmap direction, but treat patch-only merge as the production design
candidate:

1. `PlanExecutor` retrieves genuinely new material and exposes bounded material.
2. Follow-up model work is restricted to evidence extraction, not report
   rewrite.
3. Final answer selection uses a deterministic patch merge against the previous
   best `ResearchRecord`.
4. Planner usefulness is audited by quality deltas, not by source count alone.
