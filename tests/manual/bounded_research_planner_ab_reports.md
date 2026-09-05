# Bounded Research Planner A/B Reports

Generated from live manual results on 2026-08-20, 2026-08-21, and the
2026-09-05 follow-up-quality archive attempt.

This report records the 0.4.4 bounded planner experiments and later 0.5.7
follow-up quality checks. Early rows were run with production Research code
unchanged to validate the design before merging; the current harness now
exercises the production evidence-only follow-up and merge path directly. The
harness lives in
`tests/manual/bounded_research_planner_ab.py`; result JSON and trace files are
written under `tests/manual/results/`.

## Decision

Proceed with the default production implementation of evidence-only follow-up
plus deterministic merge for 0.4.4. The manual harness now calls production
`run_evidence_followup()` and production deterministic merge; the only remaining
A/B-specific execution patch is the fixture material-phase executor used to make
hidden source B available in a controlled comparison. Do not ship the older
full-report follow-up shape. Before release, validate with focused tests and at
least one real connector-backed A/B case.

0.5.7 update: the first connector-backed MiMo PubMed archive attempt did not
produce a complete baseline/planner pair, so it does not change the quality
conclusion. It does confirm that archived transcripts are now available for
diagnosis and that the remaining live problem is claim-to-evidence binding plus
provider/browser completion stability, not simply source search.

2026-09-05 browser-fetch diagnosis: the PMC failure was a production fetch-path
timing issue, not evidence that PMC is unreachable or that browser cookies are
missing. A live `BrowserSearchProvider.fetch()` read of
`https://pmc.ncbi.nlm.nih.gov/articles/PMC12064251/` now waits through the
initial cookie/challenge body and returns the article text (`len=19334` in about
4.6 seconds). The same smoke against ScienceDirect still reaches a challenge
page, but returns a bounded `page_unavailable` error in about 5.9 seconds rather
than stalling the planner. This fixes source acquisition behavior for the live
follow-up harness; it is not evidence that planner improves proof quality.

2026-09-05 clean MiMo PubMed A/B retry:
`research_followup_quality_ab-mimo-pubmed-clean-20260905-after-fetchfix.json`
remained `complete=false` after manual interruption. The archived planner
transcript reached turn 12 and parsed as a valid `done`; the missing row was
therefore after the initial Research runner reply, during pipeline follow-up
material selection or case finalization. The visible browser state showed
`https://pmc.ncbi.nlm.nih.gov/` opened from the generic follow-up search
`biomedical PubMed evidence`. A direct fetch of that PMC home page returned
usable text in about 3.1 seconds, so the issue is not an all-PMC cookie failure.
The production `PlanExecutor` now skips root landing-page URLs before opening
them as follow-up evidence material, and also rejects redirects that land on a
root home page. PMC/PubMed article URLs remain eligible.

2026-09-05 after-landingskip retry:
`research_followup_quality_ab-mimo-pubmed-clean-20260905-after-landingskip.json`
also remained `complete=false` after manual interruption. The baseline row
completed (`score=7`, `opened_target_host=true`, `proof_ok=false`,
`answer_status=partial`), while the planner transcript reached turn 16 without a
persisted planner row. The planner had already finished source acquisition by
turn 10 and then spent turns 10-16 on repeated `done`/quality-repair attempts.
The repeated blocker was a production provenance bug: legal source URLs whose
path contains balanced parentheses, such as Annals/Elsevier article IDs, were
truncated at `)`, so the quality gate falsely reported an opened URL as
unopened. The report-quality citation parser had the same boundary assumption.
Both scanners now keep balanced parentheses inside URLs and trim only unmatched
closing punctuation.

The same retry also showed that the previous landing skip was too narrow. It was
only attached to follow-up `PlanExecutor`, so the initial Research tool loop
could still show or open root home-page results such as PubMed/PMC landing pages.
The root landing filter now lives in the shared source URL selection path:
`ResearchTools.web_search()` omits these results from model-visible search
output, `ResearchTools.open_url()` skips direct or redirected root landing URLs
without recording them as sources, and the controller does not expose them as
`open_result` candidates or PubMed/arXiv priority results.

The live runs show that a planner can add value when three conditions are true:

1. The initial Research turn is kept to already visible material.
2. The planner executor can fetch genuinely new material.
3. The final result is merged as a narrow evidence patch, not as a full report
   rewrite.

The strongest current signal is the evidence-only patch-merge probe. DeepSeek,
MiMo, Qwen, StepFun, and GLM all improved `widget_noop` with one follow-up round,
one new source, one new evidence item, and no unsupported-claim regression.
Earlier runs showed that prompt-only follow-up is unstable: models may rewrite
too much, cite synthetic URLs too broadly, wrap JSON in code fences, or fail to
persist new evidence before `done`.

The 2026-08-21 trace replay check fed the five successful evidence-only3
follow-up replies into the current production `run_evidence_followup()`. All
five providers accepted the strict explicit
`{"tool":"knowledge_write","args":{...}}` shape and wrote exactly one new
evidence item, so the later schema hardening does not invalidate those
successful replies.

The 2026-08-21 post-production paired checks then exercised the actual
production evidence-only follow-up prompt and deterministic merge path. DeepSeek
kept the expected positive signal: score `5 -> 6`, coverage `0.556 -> 0.667`,
one fresh source/evidence pair, `useful=true`, and only one extra provider send.
Qwen also added the fresh source/evidence pair and improved score `5 -> 6`, but
its unsupported-claim rate regressed from `0.333` to `0.750`; the conservative
gate therefore records `useful=false` for that production-path row. StepFun
fetched the hidden fresh source, but the run stayed in a protocol/not-answered
state and the candidate was not selected, so it produced no final material gain.

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
- `bounded_research_planner_ab-deepseek-evidenceonly3-paired-widget-20260821.json`
- `bounded_research_planner_ab-mimo-evidenceonly3-paired-widget-20260821.json`
- `bounded_research_planner_ab-qwen-evidenceonly3-paired-widget-20260821.json`
- `bounded_research_planner_ab-stepfun-evidenceonly3-paired-widget-20260820.json`
- `bounded_research_planner_ab-glm-evidenceonly3-paired-widget-20260820.json`
- `bounded_research_planner_ab-deepseek-production-20260821.json`
- `bounded_research_planner_ab-qwen-production-20260821.json`
- `bounded_research_planner_ab-stepfun-production-20260821.json`

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
a stricter evidence-only merge that prevents new unsupported conclusions.

StepFun did not reach planner execution. Both arms ended with score 1 and
`answer_status=not_answered`; the planner row stopped at
`initial_stop_reason_protocol`, so there was no material gain to evaluate.

### Evidence-Only Patch Merge Runs

Files:

- `bounded_research_planner_ab-deepseek-evidenceonly3-paired-widget-20260821.json`
- `bounded_research_planner_ab-mimo-evidenceonly3-paired-widget-20260821.json`
- `bounded_research_planner_ab-qwen-evidenceonly3-paired-widget-20260821.json`
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
| DeepSeek | 5 | 6 | +1 | true | 1 | 1 -> 2 | 1 -> 2 | 3 -> 2 | 0.000 | -0.333 | +3 | +5.480s |
| MiMo | 5 | 6 | +1 | true | 1 | 1 -> 2 | 1 -> 2 | 3 -> 2 | +0.112 | -0.333 | +3 | +9.637s |
| Qwen | 5 | 6 | +1 | true | 1 | 1 -> 2 | 1 -> 2 | 3 -> 2 | 0.000 | -0.333 | +1 | +14.630s |
| StepFun | 1 | 6 | +5 | true | 1 | 1 -> 2 | 1 -> 2 | 0 -> 2 | +0.112 | 0.000 | +1 | -4.123s |
| GLM | 1 | 6 | +5 | true | 1 | 1 -> 2 | 1 -> 2 | 0 -> 2 | +0.112 | 0.000 | +1 | +40.971s |

Finding: the evidence-only shape generalizes across all five tested web
providers. DeepSeek, MiMo, and Qwen kept their previous hidden-material uplift
while removing unsupported claims from the merged record. StepFun's earlier
failure was not primarily a search/planner problem; it was the normal
follow-up/report path asking a provider with fragile long JSON `done.answer`
behavior to keep rewriting the final report. The evidence-only probe avoids that
failure mode by never asking the follow-up model to produce `done`. GLM's earlier
overclaim regression was also blocked by deterministic merge: unsupported
provider-written claims are not carried into the experimental final record.

Operational note: the first MiMo planner attempt on 2026-08-21 failed before
row execution with a stale 9222 CDP connection timeout. Restarting only the
9222 Edge listener and rerunning the pending planner row succeeded. This is a
browser-control transient, not a model/planner row failure.

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

### Post-Production Production Path Checks

| provider | baseline | planner | delta | useful | follow-up | material gain | coverage delta | unsupported delta | send delta | time delta |
|---|---:|---:|---:|---|---:|---|---:|---:|---:|---:|
| DeepSeek production | 5 | 6 | +1 | true | 1 | true | +0.111 | 0.000 | +1 | +3.984s |
| Qwen production | 5 | 6 | +1 | false | 1 | true | +0.111 | +0.417 | +1 | +8.974s |
| StepFun production | 1 | 1 | 0 | false | 0 | false | 0.000 | 0.000 | +2 | +28.387s |

The production-path rows confirm the harness is no longer measuring the old
`iteration_context` follow-up controller for rows that reach evidence-only
follow-up. DeepSeek and Qwen report
`ab_followup_mode=production_evidence_followup`, fetch
`https://source-b.test/widget-storage-update`, and merge one new evidence-backed
source. DeepSeek passes the conservative usefulness gate. Qwen does not, because
the merged result gained material and coverage but also increased the
unsupported-claim rate. StepFun fetched the same hidden source in the material
phase, but `followup_rounds=0`, `planner_stop_reason=candidate_not_selected`,
and the final record stayed at one source/evidence pair.

### Narrow Merge Projection Check

`bounded_research_merge_projection.py` was added as an offline-only diagnostic
after comparing the pre-integration evidence-only3 traces with post-production
traces. The saved A/B rows do not contain full ledger or ResearchRecord
payloads, so this is a projection rather than a production-equivalent replay.
It asks one narrow question: if the final report were rebuilt only from
evidence-backed claims and deterministic source/coverage sections, would the
paired usefulness gate improve?

The projection kept all five evidence-only3 rows useful and converted the
post-production Qwen row plus the earlier post-production StepFun row to
`useful=true`. One fresh StepFun production rerun stopped at `no_tool_calls`
before evidence-only follow-up, but it was collected after repeated StepFun
tests hit provider-side rate limiting and is treated as an invalid gate sample.
A later clean paired StepFun rerun reached fresh evidence extraction:
raw production stayed `1/false` with `candidate_not_selected`, while projection
converted the same row to `6/true` with one fresh source/evidence pair. That
supports the production `record_merge.py` narrow rebuild: when the initial
result is protocol/not_answered but the staged ledger contains evidence, the
candidate report should be rebuilt from evidence-backed claims plus
deterministic source-quality and coverage sections.

### Post-Fix Narrow Merge Validation

After the production narrow rebuild landed, Qwen was rerun on the same
`widget_noop` paired fixture:

| provider | baseline | planner | delta | useful | follow-up | sources | evidence | coverage delta | unsupported delta | send delta | time delta |
|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|
| Qwen production narrow-merge | 5 | 6 | +1 | true | 1 | 1 -> 2 | 1 -> 2 | 0.000 | -0.083 | +1 | +4.867s |

This closes the earlier Qwen regression: the old production row improved score
but failed usefulness because unsupported-claim rate regressed `0.333 -> 0.750`.
The narrow rebuild keeps the material gain and changes the paired summary to
`useful=true` with unsupported-claim rate `0.333 -> 0.250`.

### Pre-Integration Evidence-Only3 Probe

| provider | baseline | planner | delta | useful | follow-up | material gain | coverage delta | unsupported delta | send delta | time delta |
|---|---:|---:|---:|---|---:|---|---:|---:|---:|---:|
| DeepSeek evidence-only3 | 5 | 6 | +1 | true | 1 | true | 0.000 | -0.333 | +3 | +5.480s |
| MiMo evidence-only3 | 5 | 6 | +1 | true | 1 | true | +0.112 | -0.333 | +3 | +9.637s |
| Qwen evidence-only3 | 5 | 6 | +1 | true | 1 | true | 0.000 | -0.333 | +1 | +14.630s |
| GLM evidence-only3 | 1 | 6 | +5 | true | 1 | true | +0.112 | 0.000 | +1 | +40.971s |
| StepFun evidence-only3 | 1 | 6 | +5 | true | 1 | true | +0.112 | 0.000 | +1 | -4.123s |

Pre-integration signal: the design became directionally useful on all five
tested web providers only after the follow-up role was narrowed to evidence
capture and the final result was merged deterministically. That was enough to
merge the narrow production path, but not enough to claim broad real-world
research benefit; the next gate is a real connector-backed case plus continued
provider-level unsupported-claim monitoring.

### 2026-09-05 MiMo PubMed Archive Attempt

File: `research_followup_quality_ab-mimo-pubmed-archive-20260905.json`

This was the requested clean connector-backed PubMed A/B with
`--transcript-mode archive`. The output is `complete=false`, so the experiment
gate skips it and it must not be counted as a planner win or loss.

Completed row:

| arm | score | proof | answer status | coverage | sources | evidence | unsupported claims |
|---|---:|---|---|---:|---:|---:|---:|
| baseline | 7 | false | partial | 0.583 | 3 | 4 | 7/12 |

The planner arm wrote prompt/reply transcripts under
`research_followup_quality_ab-mimo-pubmed-archive-20260905.trace/transcripts/`,
but no planner result row or `case_complete` event was persisted before the run
was interrupted/stalled. The trace shows the planner reached normal source
opening and evidence writing, then repeated `done` attempts under the quality
gate. That makes the run useful for failure analysis, not for paired A/B
scoring.

Implications:

- Keep `--transcript-mode archive` for live follow-up runs; digest-only is not
  enough when a provider or browser state stalls.
- Rerun connector-backed A/B only after the provider/browser stall path is
  classified so incomplete planner rows do not keep consuming traffic.
- Tune planner selection against explicit proof gaps: missing citation, missing
  evidence ref, missing support relation, and not-evidence-backed claims.
- Do not ask the follow-up model to rewrite the final report. The useful shape
  remains new material -> evidence-only `knowledge_write` -> deterministic
  merge.
- The citation compiler can reduce wasted `done` retries from source-id formats
  like `来源s2` or `(s2)`, but it does not solve unsupported claims.

## Production Criteria Before Release

Before releasing the production default, run at least:

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

Keep the roadmap direction, with production evidence-only follow-up and
deterministic `record_merge` as the production design:

1. `PlanExecutor` retrieves genuinely new material and exposes bounded material.
2. Follow-up model work is restricted to evidence extraction, not report
   rewrite.
3. Final answer selection uses a deterministic patch merge against the previous
   best `ResearchRecord`.
4. Planner usefulness is audited by quality deltas, not by source count alone.
