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

The strongest MiMo paired run improved score from 5 to 6 with one follow-up
round, one new source, one new evidence item, and no quality regression. Earlier
runs showed that prompt-only follow-up is unstable: models may rewrite too much,
cite synthetic URLs too broadly, wrap JSON in code fences, or fail to persist
new evidence before `done`.

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
- `bounded_research_planner_ab-qwen-20260820.json`

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

The harness was changed so the normal Research model search only sees default
source A. Only the A/B `PlanExecutor` material phase can retrieve hidden source
B. This creates the intended test condition: initial answer from A, planner
execution opens B, follow-up/patch path integrates B.

| file | baseline | planner | delta | follow-up | patch | sources | evidence | coverage delta | unsupported delta | useful |
|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---|
| `hiddenmaterial` | 5 | 6 | +1 | 1 | `patch_only_merge` | 1 -> 2 | 1 -> 2 | 0.000 | -0.125 | true |
| `hiddenmaterial2` | 5 | 6 | +1 | 1 | `patch_only_merge` | 1 -> 2 | 1 -> 2 | +0.111 | -0.300 | true |

Finding: this is the first clean evidence that the desired design can help. The
second run improved coverage from 0.556 to 0.667 and unsupported claim rate from
0.800 to 0.500 with one follow-up round and one new evidence-backed source.

Cost for `hiddenmaterial2`: provider sends increased from 7 to 9 and elapsed
time increased by 54.066 seconds.

### Qwen

File: `bounded_research_planner_ab-qwen-20260820.json`

All rows failed with transient Qwen Studio send/new-chat errors, so Qwen was
excluded from usefulness conclusions.

## Current Harness Behavior

The A/B harness now tests these concepts without changing production code:

- Fresh-material execution skips already-opened URLs.
- Normal fixture search returns only default material.
- PlanExecutor fixture phase can reveal hidden new material.
- Follow-up material is recorded per run.
- Patch-only merge first prefers evidence actually written by the model.
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
   add unsupported claims or lower coverage.

The hidden-material runs separate those concerns. They show the most promising
production shape is not "planner asks model to rewrite the answer"; it is:

```text
initial ResearchRecord
  + new opened material
  + evidence-backed narrow patch
  -> deterministic merged ResearchRecord
```

## Production Criteria Before Merge

Before moving this from A/B harness into production code, run at least:

- DeepSeek hidden-material paired `widget_noop`.
- MiMo and DeepSeek hidden-material `warehouse_gap` after the fixture is tuned so
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
2. Follow-up synthesis is restricted to evidence extraction, not report rewrite.
3. Final answer selection uses a deterministic patch merge against the previous
   best `ResearchRecord`.
4. Planner usefulness is audited by quality deltas, not by source count alone.

