# Manual live benchmarks

`research_to_code_ab.py` is the Research-to-Code handoff smoke/A-B gate for
Writer-visible research context changes. It uses one synthetic coding fixture
(`discounted_total`), one synthesis note, and two arms:

- `baseline`: legacy brief render with raw excerpt and related-note id noise.
- `projection`: production structured brief render, where Key conclusions are
  backed by Citation map ids and uncited or fake-cited conclusions are demoted
  to limitations through the shared citation scanner.

The gate requires a complete matrix: for every `(case, repeat)` there must be
exactly one baseline row and one projection row. It compares success,
key-conclusion retention, trap misuse, independent verification, and the
structural `projection_trap_not_in_key_conclusions` check. By default every
prompt/reply pair is archived under the journal's `transcripts/` directory,
and the manifest is finished with `run_complete` as `done` or `failed`.

```powershell
python -B tests\manual\research_to_code_ab.py --self-test
python -B tests\manual\research_to_code_ab.py --provider deepseek --repeats 1
```

`longitudinal_research_harness_ab.py` is the 0.4.11 deterministic longitudinal
benchmark: every development case from the frozen
`tests/fixtures/research_benchmark/` corpus runs across multiple rounds
through the production projection stack (proof review, evidence runtime,
findings, gaps, brief, impact contract, capsule) and is judged by the shared
regression gate against the suite's expected observables. It verifies that
each round states only its current conclusions — superseded ones stay
relocatable by their content-addressed claim ids and are never restated as
conflicting verified constraints — that revisions refute the superseded
evidence explicitly, stale sources get flagged before a revised conclusion
counts, conflicting evidence creates findings and planner gaps, injected
unsupported claims never reach implementation constraints, and failed
analysis runs are never reported as reproduced.

```powershell
python -B tests\manual\longitudinal_research_harness_ab.py --self-test
python -B tests\manual\longitudinal_research_harness_ab.py
python -B tests\manual\longitudinal_research_harness_ab.py --case stale_claim_refresh,conflicting_evidence_gap
```

`research_comparison_benchmark_ab.py` scores three deterministic arms with the
frozen rubric: an unstructured `baseline_web_report`, an
`openscience_style_fixture` (verified locators and support relations, no
counterevidence pass, no reproducible analysis), and the full
`codey_evidence_loop`. Without a schema-valid real head-to-head artifact its
summary may only say "OpenScience-style regression passed", and only when the
comparison verdict itself passed. The artifact must be JSON containing every
roadmap metadata field — both sides' version/commit, provider/model, task
inputs, run date, result source, a `rubric` equal to the frozen suite rubric
name plus a matching `rubric_digest` taken from the suite lock — *and* its
recorded result must back the wording:
`winner: "codey"`, `strictly_better_metric_count >= 4`, and
`regression_gates_passed: true`.
`--openscience-artifact <file>` together with `--claim-superiority` is the
only way the summary may contain "surpassed OpenScience"; the digest and the
validated result fields are recorded alongside the claim, incomplete,
oversized, or opposing-result artifacts fail closed with a non-zero exit
instead of unlocking anything, and each summary shows
`codey_commit_alignment` — whether the artifact's recorded Codey commit is
the current HEAD (informational; recorded runs stay valid evidence as code
moves on).

```powershell
python -B tests\manual\research_comparison_benchmark_ab.py --self-test
python -B tests\manual\research_comparison_benchmark_ab.py --output tests\manual\results\research_comparison_benchmark_deterministic.json
```

The frozen corpus itself is guarded offline by
`research_benchmark_suite.py`: split integrity (development vs held-out),
fixture path containment, regression-gate vocabulary alignment, rubric weights,
and lock hashes. `--update-lock` is the one explicit escape hatch for
intentional fixture changes.

```powershell
python -B tests\manual\research_benchmark_suite.py
python -B tests\manual\research_benchmark_suite.py --update-lock
```

Shared plumbing for these harnesses (journaling provider wrapper, interleaved
arm schedules, complete-matrix checks, atomic JSON persistence, resume
payloads with provider identity guards, and the fixture search provider) lives
in `ab_harness_common.py`; production code must never import it.

0.4.11 provider-smoke boundary: `longitudinal_research_harness_ab.py` and
`research_comparison_benchmark_ab.py` are deterministic-only and intentionally
have no `--provider` mode yet. For the provider-enabled harnesses, treat Qwen
live smoke as diagnostic unless each arm was isolated in a clean provider
state. A 2026-08-24 paired `bounded_research_planner_ab.py` Qwen run completed
the baseline row but the planner row failed after one send with no model reply
while Qwen Studio stayed in its native web-search UI; a planner-only rerun on
the same fixture completed (`score=6`, one follow-up round, two sources). When
diagnosing Qwen stalls, rerun one arm per fresh chat or use
`qwen_submit_probe.py` before treating the paired result as release evidence.


`changeset_review_ab.py` compares the old path-only Review prompt with the
current ChangeSet-summary prompt. It sends fixed review-only diffs to one live
provider at a time and scores whether the model catches the seeded issue,
uses a changed path, and returns a valid optional hunk/line anchor:

```powershell
python -B tests\manual\changeset_review_ab.py --self-test
python -B tests\manual\changeset_review_ab.py --provider deepseek --timeout 90
python -B tests\manual\changeset_review_ab.py --provider qwen --timeout 90
python -B tests\manual\changeset_review_ab.py --provider stepfun --timeout 90
python -B tests\manual\changeset_review_ab.py --provider glm --timeout 90
```

Run providers one at a time to keep web stalls isolated. The script writes its
JSON report to the system temporary directory by default.

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
- `zoom`: production `ProjectTaskContextBuilder` Project Map, including the
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
Lens. It compares the current production `ProjectTaskContextBuilder` Project
Map against a prototype that replaces Focused subtree / Symbol overview with a
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
python -B tests\manual\edit_failure_context_ab.py --provider stepfun --port 9222
```

`default_verification_ab.py` compares the pre-0.1.35 completion behavior with
the bounded production policy for trusted post-edit verification. Run one
case/arm at a time to avoid provider rate limits:

```powershell
python -B tests\manual\default_verification_ab.py `
  --provider stepfun `
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
background repair helper can open a fresh tab in its dedicated isolated
profile (`state_home/self-repair/<provider>`, never the user's default
browser profile), and that a candidate Provider worker can run a neutral
marker canary:

Login note: the smoke exercises two isolated profiles -- the repair helper
(`state_home/self-repair/<provider>`) and the candidate worker
(`state_home/provider-workers/<provider>`, stable across override
generations). Both start without cookies, so the first smoke per provider
requires a manual login in each tab it opens; a failed marker check on an
untouched profile usually means "not logged in yet", not a driver
regression.

```powershell
python -B tests\manual\adapter_self_repair_smoke.py --provider qwen --timeout 90
python -B tests\manual\adapter_self_repair_smoke.py --provider deepseek --timeout 90
python -B tests\manual\adapter_self_repair_smoke.py --provider stepfun --timeout 90
python -B tests\manual\adapter_self_repair_smoke.py --provider glm --timeout 90
```

Reports contain only bounded status metadata, marker length/exactness, timing,
and error type/message snippets. They do not store prompts, replies, cookies,
page text, DOM, or project data.

`python_syntax_regression_ab.py` compares the production Python
syntax-regression hint with a baseline that suppresses it. Fault injection is
probe-only: it inserts the same missing colon in both A/B arms and runs a
separate valid-edit control. Production parsing is skipped above 128K
characters; this is a character budget, not a byte-size limit:

```powershell
python -B tests\manual\python_syntax_regression_ab.py --provider deepseek
python -B tests\manual\python_syntax_regression_ab.py --provider qwen
python -B tests\manual\python_syntax_regression_ab.py --provider stepfun
python -B tests\manual\python_syntax_regression_ab.py --provider glm
```

The default order is baseline-first for DeepSeek/Qwen and hint-first for
StepFun/GLM. Use `--order baseline-first` or `--order hint-first` to override it.

`refactor_hint_ab.py` compares current production edits with a probe-only
incomplete-refactor hint. The hint arm injects an edit wrapper only inside the
script: after a narrow identifier rename, it runs a
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

`impact_guard_ab.py` is a newer probe-only harness for a post-edit Impact Guard.
The guard arm wraps `edit_file` only inside the script: after a changed
definition is detected, it runs a bounded read-only lexical reference scan and
appends a short `path:line` note marked as not coverage proof. It does not
change production prompts, tools, UI, or verification behavior:

```powershell
python -B tests\manual\impact_guard_ab.py --self-test
python -B tests\manual\impact_guard_ab.py `
  --provider deepseek `
  --case python-function-rename `
  --case ts-exported-function-rename `
  --out tests\manual\results\impact_guard_deepseek.json
```

The July 2026 live A/B found a strong TypeScript exported-function rename win,
but Python rename was already handled by current production and the sample was
too small. Keep this as a manual probe only; do not promote it to production
without broader Python/refactor evidence and clean control cases.

`review_impact_map_ab.py` is a review-only A/B probe for the production Review
Impact Map. The `impact_map` arm builds a short, bounded caller/test reference
hint from temporary fixture files and passes it through the production review
prompt path. It does not change Writer behavior, tools, UI, or verification:

```powershell
python -B tests\manual\review_impact_map_ab.py --self-test
python -B tests\manual\review_impact_map_ab.py `
  --provider qwen `
  --timeout 90 `
  --output tests\manual\results\review_impact_map_qwen.json
```

Run one provider per process when collecting live results. The scorer tracks
`issue_hit`, changed-path compliance, whether the reviewer mentions affected
callers/tests, false-positive review on a safe control case, and prompt-size
delta. Keep this as a regression probe for affected-caller/test awareness and
false-positive review noise.

`deep_research_core_ab.py` is a Research-only A/B probe for possible Deep
Research Core changes. Production Research now includes `source_search`; this
probe still keeps a source-search-free baseline arm so the locator can be
compared against the older behavior. The live provider runs a real JSON-tool
research loop, but search results, source bodies, PDF pages, and local-memory
notes are deterministic fixtures.
The probe prompt explicitly tells web models not to use the chat site's built-in
web search or outside knowledge, because fixture URLs may not exist publicly.

Arms:

- `baseline`: production Research prompt with `source_search` disabled.
- `source_search`: production Research prompt and production `source_search`
  locator tool for already-opened sources.
- `deep_core`: `source_search` plus an experimental compact Research Plan /
  Coverage prompt.

```powershell
python -B tests\manual\deep_research_core_ab.py --self-test
python -B tests\manual\deep_research_core_ab.py `
  --provider deepseek `
  --profile cheap `
  --timeout 90 `
  --open-if-missing `
  --output tests\manual\results\deep_research_core_deepseek_cheap.json
```

Default `--profile cheap` keeps live traffic bounded: two high-signal fixture
cases, all arms, and a 10-turn cap. Use `--profile full` only when intentionally
running all fixture cases with the normal 14-turn cap.
Use `--single-tool-boundary` only for manual provider diagnosis. It repeats the
one-tool-per-turn Research boundary in the fixture front matter and reminders so
providers such as MiMo can be compared against older runs that emitted several
tool calls in one reply.

The scorer tracks primary-source opening, source_search use, target
page/offset recall, exact evidence snippets, counter/limitations reporting,
local-memory reuse, unsupported citations, turn count, and max-turn failures.
It also stores send/reply counts, done attempts, quality-repair prompt counts,
and the last raw reply previews so provider/protocol failures can be diagnosed
without rerunning the model. During live runs it also atomically writes an
incremental trace next to the output file, for example
`deep_research_core_deepseek_cheap.trace.json`, after each provider reply.
By default it only attaches to already-open provider tabs; add
`--open-if-missing` when intentionally allowing the probe to open or foreground
provider pages. Use this probe before changing source_search behavior further,
or before promoting ResearchPlan / Coverage Review into the production Research
path.

`bounded_research_planner_ab.py` is the 0.4.4 bounded Research planner A/B
probe. The baseline arm runs the production ResearchPipeline with follow-up
disabled; the planner arm enables one bounded follow-up round, leaves the
wall-clock limiter off, and calls production `run_evidence_followup()` plus the
production deterministic merge. The only remaining A/B-specific execution
piece is the fresh-material fixture executor that exposes hidden source B and
skips already-opened URLs. It uses deterministic fixture search documents and a
live provider, and it records atomic send/reply trace rows plus a paired
`followup_usefulness` summary with coverage, unsupported-claim, evidence,
source, query, fetch, send, and time deltas. The summary separates
`execution_material_gain` (the planner fetched a previously unread source) from
`material_gain` (the final ResearchRecord gained sources or evidence).
`useful=true` is intentionally conservative: both rows must complete
successfully, follow-up must run, final-record material must appear, quality
must improve, and coverage/status/unsupported-claim score must not regress.

```powershell
python -B tests\manual\bounded_research_planner_ab.py --self-test
python -B tests\manual\bounded_research_planner_ab.py `
  --provider deepseek `
  --case warehouse_gap `
  --case widget_noop `
  --output tests\manual\results\bounded_research_planner_ab-deepseek.json
```

For low-cost live smoke after harness refactors, prefer one provider and one
case. With Qwen, run arms separately or restart the provider chat between arms
when the website's native web search is active; a failed send before any
fixture query/fetch is a provider-state failure, not evidence that the planner
loop itself is stuck.

2026-08-20 live runs:

- DeepSeek: `warehouse_gap` went from score `4` to `8` with `planner_stop_reason=no_actionable_gap`; `widget_noop` stayed at `5` with one follow-up round, `+18.166s`, and `+1` provider send, but no new sources or evidence.
- MiMo: `warehouse_gap` went from score `4` to `6`; `widget_noop` stayed at `5`; both cases hit `max_wall_time` before any follow-up round, so no new material was added.
- Artifacts: `tests\manual\results\bounded_research_planner_ab-deepseek-20260820.json`, `tests\manual\results\bounded_research_planner_ab-mimo-20260820.json`.

After disabling the planner-arm wall-clock limiter in the probe, MiMo reran to
`tests\manual\results\bounded_research_planner_ab-mimo-nowall-20260820.json`:
`warehouse_gap` improved from score `6` to `8` but still stopped at
`no_actionable_gap` with no follow-up round; `widget_noop` ran one follow-up
round and stayed at score `5`, with `+3` queries, `+1` fetch, `+2` provider
sends, and no new sources or evidence. This confirms wall time was masking one
case but was not the core planner-value problem.

With the A/B-only fresh-material executor, MiMo reran to
`tests\manual\results\bounded_research_planner_ab-mimo-freshmaterial-20260820.json`:
`warehouse_gap` stayed `4 -> 4` and correctly reported `no_new_material`;
`widget_noop` stayed `5 -> 5`, fetched one previously unread source
(`https://standards.example.org/widget-storage-update`), improved coverage by
`+0.111` and unsupported-claim rate by `-0.467`, but the final ResearchRecord
still gained `0` sources and `0` evidence items. This shows the fresh-material
executor is finding new material; the remaining gap is follow-up synthesis
absorption, not search dedupe.

Later hidden-material paired runs moved the useful experiment condition into a
cleaner shape: normal Research search sees only source A, while the A/B
PlanExecutor material phase can reveal hidden source B. The current
`widget_noop` web-provider summary is recorded in
`tests\manual\bounded_research_planner_ab_reports.md`: DeepSeek, MiMo, and Qwen
each improved from score `5` to `6` with one new evidence-backed source. The
2026-08-21 evidence-only patch-merge rerun kept that uplift on DeepSeek, MiMo,
and Qwen, and the same evidence-only shape recovered GLM and StepFun on
`widget_noop`: all five tested web providers now show `useful=true`, one fresh
source/evidence pair, and no unsupported-claim regression by limiting follow-up
to `knowledge_write` and compiling the final report deterministically.

A trace replay check then fed the five successful evidence-only3 follow-up
replies back into the current production `run_evidence_followup()`. DeepSeek,
MiMo, Qwen, StepFun, and GLM all passed the strict explicit
`{"tool":"knowledge_write","args":{...}}` schema and wrote one new evidence
item, confirming that the schema hardening itself does not invalidate those
successful model replies. New live A/B rows now exercise the production
follow-up prompt directly rather than the older harness-only controller.

The 2026-08-21 post-production `widget_noop` reruns use that production
follow-up path directly. DeepSeek improved score `5 -> 6`, added one fresh
source/evidence pair, improved coverage `0.556 -> 0.667`, and stayed
`useful=true` with one extra provider send. Qwen also improved score `5 -> 6`
and added the same fresh source/evidence pair, but its unsupported-claim rate
regressed `0.333 -> 0.750`, so the conservative paired summary correctly keeps
`useful=false` for that row. StepFun fetched the hidden fresh source during the
material phase, but the final run stayed protocol/not-answered, selected no
candidate, and remained `useful=false`.

`bounded_research_merge_projection.py` is an offline-only diagnostic for the
0.4.4 merge shape. It reads saved bounded-planner A/B JSON plus trace files and
projects whether an evidence-only final report rebuild would improve the paired
`followup_usefulness` result. It does not replay a provider and does not rebuild
the full Research ledger, so treat it as a hypothesis check before changing
production code.

```powershell
python -B tests\manual\bounded_research_merge_projection.py --self-test
python -B tests\manual\bounded_research_merge_projection.py `
  --output tests\manual\results\bounded_research_merge_projection-20260821.json
```

On the saved five-provider evidence-only3 rows plus the three post-production
rows, the projection kept the five old useful rows useful and converted Qwen
production and the earlier StepFun production row to useful. One fresh StepFun
rerun stopped before evidence-only follow-up while StepFun was rate-limited, so
that row is an invalid gate sample. A later clean paired StepFun rerun showed
the raw production path could fetch and write fresh evidence but still left the
candidate unselected; the projection converted that paired row to
`projected=6/useful=true`. This is the validating signal for the production
`record_merge.py` narrow rebuild: protocol/not_answered summaries are rebuilt
from ledger evidence, source quality, and coverage instead of inheriting stale
model-written sections.

A post-fix Qwen paired rerun on the same fixture confirms the intended
production effect: score `5 -> 6`, `useful=true`, sources/evidence `1/1 -> 2/2`,
coverage unchanged at `0.667`, unsupported-claim rate `0.333 -> 0.250`,
provider sends `5 -> 6`, and wall time `44.521s -> 49.388s`. This replaces the
older Qwen production row where unsupported-claim rate regressed to `0.750`.

The latest production hardening does not change the A/B prompt surface. It
removes avoidable measurement and transaction noise around the experiment:
`PlanExecutor` stops before any extra search once the source budget is full,
staged note links resolve by normal note title before commit and restore touched
SQLite link edges on rollback, deterministic merge shares the report-quality
citation parser, and non-model merge assembly no longer increments Research
turn counts.
The evidence-only write boundary is now even narrower in production: follow-up
`knowledge_write` accepts only `type/title/body/sources/evidence`, requires
`sources` and `evidence` to be explicit non-empty lists, rejects `source` as a
`source_url` alias, staged rollback uses the public `KnowledgeChanges` snapshot
API, link snapshot restore filters rows to the touched note ids, and
deterministic merge preserves project metadata for merged records.

Use this probe when you want to judge whether 0.4.4 follow-up search is adding
real value or just extra traffic. The paired summary is the main signal; if
coverage and unsupported-claim rate barely move while time and traffic grow,
the planner arm is not paying for itself.

`source_connector_ab.py` is the live Research probe for the PubMed/arXiv
connector-aware search path. The baseline arm uses the same non-isolated
Research browser search provider reuse path as production Research; the
connector arm wraps that provider with
`ConnectorAwareSearchProvider`. The model-visible controller actions are
`web_search/open_result/reopen_source/open_hit/source_search/knowledge_write/done`;
the runner records both those raw model actions and the compiled runtime tool
calls so protocol regressions are visible without rerunning completed rows. In
addition, each prompt/reply pair is written atomically to a sibling
`*.trace.json` file (defaulting to the output stem) so repeated `done` attempts
can be replayed later against the exact prompt text.

```powershell
python -B tests\manual\source_connector_ab.py --self-test
python -B tests\manual\source_connector_ab.py `
  --provider deepseek `
  --case pubmed `
  --case arxiv `
  --case open_guard `
  --output tests\manual\results\source_connector_ab-deepseek-0.4.3.json
```

Run one provider per process. By default the probe only attaches to already-open
provider tabs; add `--open-if-missing` only when intentionally allowing Codey to
open or foreground that provider page. Rows are written atomically after each
case/arm, and reruns skip completed rows unless `--rerun-failed` is set. The
trace file is written next to the output file by default, or to
`--trace-output` when set.

`source_connector_done_ab.py` is the companion done-stage probe. It keeps the
same live connector setup, but compares prompt/checklist strategies with
run-level stats for first-pass and eventual pass rates. Production Research
already runs the narrow citation compiler before the quality gate, so every arm
uses the same compiler; the current probe varies only the model-facing boundary
and batched quality-review prompts. Use `--samples N` to repeat each case/arm N
times.

The same narrow citation compiler now runs in production `done` handling
before the quality gate; it compiles sources, but it does not add new support
or weaken the blocker checks. Source-id citations and parsed numeric citations
are compiled through separate bindings, and bracket text outside the citation
grammar is left untouched. Numeric drift can be normalized when parsed old
source rows all dedupe to one canonical URL, including repeated rows for the
same source; ambiguous multi-source drift is left to the repair loop. Internal
source-id leakage checks cover pre-heading prose plus the report body. The
quality gate scans `## 来源` line by line: parsed source rows protect title
text such as literal `[S1]`, while non-source notes such as `note [s9]` and
contextual leaks such as `source_id=s9` are still blockers. When a report has
no citable sources, the compiler re-renders the sectioned report and drops any
preamble before handing the result to the quality gate.

```powershell
python -B tests\manual\source_connector_done_ab.py --self-test
python -B tests\manual\source_connector_done_ab.py `
  --provider qwen `
  --case pubmed `
  --arms baseline,boundary,batch `
  --samples 3 `
  --open-if-missing `
  --output tests\manual\results\source_connector_done_ab-qwen-pubmed.json
```

Run this on one provider at a time. The probe writes per-run rows so the JSON
summary can compare first-pass vs eventual-pass behavior across repeated live
runs.

2026-08-19 MiMo PubMed done-stage sample from the pre-production finalizer
probe:

- Baseline from `source_connector_done_ab-mimo-pubmed-max24.json`: score `9`,
  connector-valid, `done_attempts=2`, `quality_retry_count=1`,
  `first_done_passed=false`, `eventual_done_passed=true`.
- Finalizer from a clean single-arm process
  `source_connector_done_ab-mimo-pubmed-finalizer-only.json`: score `9`,
  connector-valid, `done_attempts=1`, `quality_retry_count=0`,
  `first_done_passed=true`, `eventual_done_passed=true`,
  `finalizer_rewrites=1`.
- Full baseline/finalizer report text is archived in
  `tests/manual/source_connector_done_ab_mimo_pubmed_reports.md`.

These historical rows were used to justify the production citation compiler.
They are not a current no-compiler vs compiler A/B template because the compiler
now runs in production for every arm.

2026-08-19 Qwen PubMed done-stage sample from the pre-production finalizer
probe:

- Baseline from `source_connector_done_ab-qwen-pubmed-baseline-20260819-134023.json`:
  score `5`, `done_attempts=2`, `quality_retry_count=1`,
  `first_done_passed=false`, `eventual_done_passed=true`,
  `connector_valid=false`, `opened_target_host=false`.
- Finalizer from a clean single-arm process
  `source_connector_done_ab-qwen-pubmed-finalizer-20260819-134023.json`:
  score `5`, `done_attempts=1`, `quality_retry_count=0`,
  `first_done_passed=true`, `eventual_done_passed=true`,
  `connector_valid=false`, `opened_target_host=false`,
  `finalizer_rewrites=1`.
- Full baseline/finalizer report text is archived in
  `tests/manual/source_connector_done_ab_qwen_pubmed_reports.md`.

The isolated finalizer rerun improved first-pass done success and reduced the
turn budget, but both arms still missed the target host and proof-quality
remained partial. For Qwen, the historical experiment mainly confirmed that
deterministic citation compilation can help done-stage stability even when
connector selection is weak.

The production connector path builds PubMed/arXiv API queries from bounded safe
terms, masks direct and natural-language secret marker/value windows such as
`api key ...`, `api key is ...`, `password is equal to ...`,
`password is set to ...`, `password is configured as ...`,
`client secret known as ...`, `api key called ...`, `api key named ...`,
over-padded, multi-token, or punctuation-separated connector phrases such as
`password is configured as known as called ...` and
`password - is - configured - as - known - as - called - ...`, `密码 是 ...`, and `密钥等于 ...`
plus `access_token ...`, `passphrase ...`, and value-shaped contextual followers
such as `token abcdef`, `cookie abcdef`, and `jwt abcdef` before source API
requests, keeps cleaned domain terms when available, reuses one safe connector
query for routing and request assembly, starts browser search before connector
lookup, and falls back to browser fetch when direct PubMed/arXiv connector
lookup fails.
For 0.4.3, the live connector smoke was run one provider at a time and used the
atomic rows to resume only missing samples. DeepSeek showed PubMed connector
search improving target source selection; MiMo and StepFun connector arms opened
PubMed target hosts; Qwen improved on arXiv after the provenance fix; and
DeepSeek/Qwen/MiMo/StepFun/GLM reached arXiv target hosts in at least one
recorded arm. Several runs still stopped at `max_turns` or protocol repair, and
GLM PubMed remained inconclusive after repeated attempts hit provider rate
limits. The browser search provider now reuses one dedicated Research
profile/port for ordinary runs instead of opening isolated search browsers.

`research_repair_prompt_ab.py` is a tiny live probe for Research protocol repair
wording. It sends the old and current repair prompts to a provider, scores the
reply with the production Research parser/controller, and atomically writes a
JSON result after each arm. It does not execute Research tools or write vault
notes.

```powershell
python -B tests\manual\research_repair_prompt_ab.py --self-test
python -B tests\manual\research_repair_prompt_ab.py `
  --provider mimo `
  --port 9222 `
  --timeout 120 `
  --order old,new `
  --keep-open
```

`coding_repair_prompt_ab.py` is the matching coding-side probe for production
typed protocol repairs. It sends deliberately invalid coding replies
(`write_file`, mixed edit modes, bad read offsets, direct prose answers,
native-tool denial, and nested tool JSON inside `done.summary`) and compares
the legacy generic repair prompt with Codey's production typed repair prompt
(`agent._protocol_repair_prompt`). It
scores whether the next reply is accepted by the production coding codec,
matches the expected action, and is a strict single JSON object. It does not
execute local tools or edit project files.

```powershell
python -B tests\manual\coding_repair_prompt_ab.py --self-test
python -B tests\manual\coding_repair_prompt_ab.py `
  --provider mimo `
  --port 9222 `
  --timeout 120 `
  --keep-open
```

2026-07-28 live A/B now measures the production repair prompt directly. An
earlier prototype run showed the same direction but included ideal repaired
shapes, so it is treated as over-strong directional evidence. The production
prompt now generates previous-intent examples from the invalid JSON itself. The
production probe ran the same six invalid coding replies against already-open
web-model tabs and did not execute any local tools:

- DeepSeek: baseline `clean_repair=5/6`, typed `clean_repair=6/6`.
- Qwen: baseline `clean_repair=4/6`, typed `clean_repair=6/6` after rerunning
  one transient baseline send failure for `invalid_edit_mixed_modes`.
- MiMo: baseline `clean_repair=5/6`, typed `clean_repair=6/6`.

The observed wins were exact argument repair, not broader planning: typed
repair preserved edit newlines where the generic baseline often dropped them,
and repaired invalid `read_file offset=0` into a 1-based page request while
keeping the original `limit=120`. This evidence supports typed coding protocol
repairs only; it does not change coding's existing
multiple-top-level-JSON compatibility behavior, add an allowed-tools gate, or
introduce verification candidate IDs.

`ghost_signal_extractor_ab.py` is a Ghost-only A/B probe for 0.3.0's explicit
learning signal extractor. The `baseline` arm emits no signals; the
`extractor` arm asks one live provider at a time to classify current user
messages into candidate signals such as style preferences, corrections,
research interests, long-term goals, and action tendencies. It does not execute
local tools, write accepted memory, inject a Ghost directive, or change
production chat/coding/Research behavior.

```powershell
python -B tests\manual\ghost_signal_extractor_ab.py --self-test
python -B tests\manual\ghost_signal_extractor_ab.py `
  --provider qwen `
  --port 9222 `
  --timeout 90
```

Run providers one process at a time. The scorer tracks kind hits, no-signal
false positives, JSON parse success, and whether every `evidence_quote` is
grounded in the user message.

2026-08-06 live A/B after prompt tightening:

- DeepSeek: extractor `7/7`, explicit signals `5/5`, no-signal controls `2/2`,
  grounded quotes `7/7`; baseline `2/7`.
- Qwen: extractor `7/7`, explicit signals `5/5`, no-signal controls `2/2`,
  grounded quotes `7/7`; baseline `2/7`.
- MiMo: extractor `7/7`, explicit signals `5/5`, no-signal controls `2/2`,
  grounded quotes `7/7`; baseline `2/7`.
- StepFun: extractor `7/7`, explicit signals `5/5`, no-signal controls `2/2`,
  grounded quotes `7/7`; baseline `2/7`.
- GLM: extractor `7/7`, explicit signals `5/5`, no-signal controls `2/2`,
  grounded quotes `7/7`; baseline `2/7`.

The model-visible extractor prompt is intentionally generic and does not expose
internal product names. DeepSeek and Qwen initially showed useful boundary
failures (`action_tendency` vs `correction`, then `style_preference` vs
`action_tendency`); the current prompt fixes those distinctions.

Privacy note: candidate signals that look like passwords, API keys, bearer
tokens, private keys, or high-entropy secrets are rejected by the schema parser
before they can be written to `state_home/ghost/signals.jsonl`.

CDP note: this probe now always releases non-isolated Playwright automation,
even when a caller passes `--keep-open`; regular `Session.close()` leaves reused
provider tabs open. This avoids half-stale CDP attachments between one-provider
manual runs. If `/json/version` responds but Playwright attach stalls, Codey
fails fast instead of silently switching to another provider port, because the
opened port may be the one with the user's logged-in provider tabs.

Failure-path note: provider/CDP connection failures are written to the JSON
output as bounded failure rows and the probe exits non-zero. The probe should not
mask the original web-provider failure with its own reporting error.

`ghost_directive_ab.py` is a Ghost-only A/B probe for 0.3.3's bounded prompt
context. The `baseline` arm sends the task normally; the `directive` arm prepends
the same short neutral `Local Context` that production chat/planning can receive
from confirmed Hebbian state. The model-visible prompt must not contain `Ghost`
or `Ghost Directive`; directive items are rendered from an explicit typed-field
allowlist, not raw labels or arbitrary slugs, and the renderer treats sensitive secret-like text or
instruction-hierarchy attacks as non-renderable memory. It runs one provider per
process and checks correction hits, context leakage, and `planning_readonly`
JSON protocol compliance. It does not edit files, call local tools, write Ghost
state, touch Research, or enable Project Writer directive injection.

```powershell
python -B tests\manual\ghost_directive_ab.py --self-test
python -B tests\manual\ghost_directive_ab.py `
  --provider deepseek `
  --port 9222 `
  --timeout 90 `
  --new-chat-timeout 45 `
  --output tests\manual\results\ghost_directive_deepseek.json
```

Run providers one at a time. A failure row means the provider/CDP path or the
model response did not satisfy the narrow probe; it should not be hidden by
retrying all providers in one batch.

`ghost_learning_loop_ab.py` is a Ghost-only A/B probe for 0.3.4's post-turn
learning loop. It runs against a temporary `state_home`, sends a baseline chat
prompt, teaches an explicit typed style preference in a separate learning turn,
uses a fresh provider tab for extraction, and then sends the same task with the
newly learned neutral `Local Context`. It checks that the typed style preference
is accepted/reinforced, that a plain complaint such as "you are wrong" does not
become accepted memory, that the directive text changes, and that model replies
do not leak internal Ghost naming. It does not edit files, scan a project, call
Research, enable Project Writer learning, or change permissions.

```powershell
python -B tests\manual\ghost_learning_loop_ab.py --self-test
python -B tests\manual\ghost_learning_loop_ab.py `
  --provider deepseek `
  --port 9222 `
  --timeout 90 `
  --new-chat-timeout 45 `
  --output tests\manual\results\ghost_learning_loop_deepseek.json
```

Run one provider per browser process. Restart the 9222 Edge CDP session between
providers so stale pages, half-attached Playwright sessions, and unfinished
extractor tabs cannot contaminate the next result.

The live harness opens the extractor in a temporary sibling tab from the same
provider browser context. That keeps the extractor prompt out of the user's
current chat tab and avoids nested Playwright sync attachments in the manual
process; production still receives its provider factory from the server.

2026-08-09 Ghost Learning Loop live A/B, run one provider per restarted Edge CDP
session:

- DeepSeek: passed. The learning loop accepted/reinforced `reply_length=concise`
  and `reply_structure=answer_first`; the next answer was shorter and did not
  leak internal naming.
- MiMo: passed. It produced one extra candidate row, but only the two safe typed
  style preferences became active Hebbian nodes.
- Qwen: passed. The directive arm was much shorter than baseline and preserved
  the typed local context without internal naming leakage.
- GLM: passed after a scoped restart of the 9222 Edge CDP session.
- StepFun: passed. It also produced one extra candidate row, but only the two
  renderable typed preferences were active.

`ghost_router_ab.py` and `ghost_router_production_ab.py` cover 0.3.7 automatic
task routing. The router-only probe asks a live provider for one JSON route
decision. The production-spine probe runs the real `TaskRunner` routing entry
point with safe mode-body stubs, so it verifies `task_start.mode`,
`task_done.mode`, and dispatch without editing the repository or running shell
commands.

```powershell
python -B tests\manual\ghost_router_ab.py --self-test
python -B tests\manual\ghost_router_ab.py `
  --provider deepseek `
  --port 9222 `
  --timeout 90 `
  --new-chat-timeout 45 `
  --output tests\manual\results\ghost_router_deepseek.json

python -B tests\manual\ghost_router_production_ab.py --self-test
python -B tests\manual\ghost_router_production_ab.py `
  --provider deepseek `
  --port 9222 `
  --output tests\manual\results\ghost_router_production_deepseek.json
```

Run one provider per restarted Edge CDP session. The score weights
Writer/Hybrid confusion more heavily than Chat/Planning confusion because mode
errors have different blast radii. Production A/B reports are written
atomically after each case with `complete=false` until the full run finishes.
The fixture also includes a project-attached chat regression where the user
explicitly forbids project file access; production code must keep that case in
chat even if the router model selects Writer.

`ghost_work_queue_production_ab.py` covers 0.3.8 Work Queue continuation. It
uses the production `TaskRunner` entry point and real queue claim/complete/block
transitions, while mode bodies are safe stubs so the probe does not edit the
repository or run shell commands. The live provider is only used as the normal
main provider for paths that need it; Work Queue itself does not call a model.
Since 0.4.2, the research safe stub also writes a real `ResearchRecord` into
the Evidence Ledger so queued Research completion is tested through the
`research_proof:<digest>` gate rather than a legacy `research:*` ref.

```powershell
python -B tests\manual\ghost_work_queue_production_ab.py --self-test
python -B tests\manual\ghost_work_queue_production_ab.py `
  --provider deepseek `
  --port 9222 `
  --output tests\manual\results\ghost_work_queue_production_deepseek.json
```

Run one provider per restarted Edge CDP session when doing live smoke. The
report is written atomically after each case with `complete=false` until the
full run finishes, so interrupted provider runs still show which rows completed.
The matrix verifies that no queued item leaves "continue" as ordinary chat, a
research item enters Research, a project follow-up enters Writer, a review item
enters review-only, and a contentful request such as "continue researching
pytest changes" does not consume the queue.

2026-08-11 Ghost Work Queue production-spine A/B, five-case matrix, run one
provider per restarted Edge CDP session:

- DeepSeek: baseline 4/5; queue 5/5.
- Qwen: baseline 4/5; queue 5/5.
- MiMo: baseline 4/5; queue 5/5.
- GLM: baseline 4/5; queue 5/5.
- StepFun: baseline 4/5; queue 5/5.

The baseline miss was the intended Research follow-up: without the queue,
"continue" stayed in Chat; with the queue, Codey claimed the saved item and
dispatched Research. Output JSON files were written under
`tests/manual/results/ghost_work_queue_production_*.json`.

2026-08-17 0.4.2 Research Proof Quality Gate smoke, one provider at a time
against Edge CDP 9222, scoped to the Research queue row:

- DeepSeek: `ghost_work_queue_production_0_4_2_deepseek_research_item.json` ok.
- Qwen: `ghost_work_queue_production_0_4_2_qwen_research_item.json` ok.
- MiMo: `ghost_work_queue_production_0_4_2_mimo_research_item.json` ok.
- StepFun: `ghost_work_queue_production_0_4_2_stepfun_research_item.json` ok.
- GLM: `ghost_work_queue_production_0_4_2_glm_research_item.json` ok.

The first DeepSeek attempt failed before TaskRunner because CDP port 9222 was
not open. After the Edge CDP session came up, DeepSeek was rerun atomically and
passed.

`ghost_research_interest_queue_production_ab.py` covers 0.3.9 Research
Interest Queue consumption. Candidate generation is deterministic and local:
Research note structured `open_questions` and structured Concept Graph missing
links are harvested into existing Work Queue items. The harness uses the production
`TaskRunner` claim path, while mode bodies are safe stubs so it does not edit
files or run shell commands. Since 0.4.2, the `research_proof=true` rows build
a bounded `ResearchRecord`, append it to the Evidence Ledger, and verify queue
completion through the Research Proof Quality Gate; `research_proof=false`
still verifies blocking.

```powershell
python -B tests\manual\ghost_research_interest_queue_production_ab.py --self-test
python -B tests\manual\ghost_research_interest_queue_production_ab.py `
  --provider deepseek `
  --port 9222 `
  --output tests\manual\results\ghost_research_interest_queue_production_deepseek.json
```

Run one provider per restarted Edge CDP session. The matrix verifies no-queue
fallback, Research note structured-open-question dispatch, strong concept missing-link
dispatch, weak concept missing-link non-consumption, contentful continuation
non-consumption, and missing Research proof blocking the item.

2026-08-12 Research Interest Queue production-spine A/B, six-case matrix, run
one provider per restarted Edge CDP session:

- DeepSeek: baseline 3/6; queue 6/6.
- Qwen: baseline 3/6; queue 6/6.
- MiMo: baseline 3/6; queue 6/6.
- StepFun: baseline 3/6; queue 6/6.
- GLM: partial, baseline 2/4; queue 4/4. The remaining cases timed out because
  the webpage was slow/self-searching.

2026-08-17 0.4.2 Research Proof Quality Gate smoke, one provider at a time
against Edge CDP 9222, scoped to proof/no-proof queued Research completion:

- DeepSeek: `ghost_research_interest_queue_production_0_4_2_deepseek_gate.json` ok.
- Qwen: `ghost_research_interest_queue_production_0_4_2_qwen_gate.json` ok.
- MiMo: `ghost_research_interest_queue_production_0_4_2_mimo_gate.json` ok.
- StepFun: `ghost_research_interest_queue_production_0_4_2_stepfun_gate.json` ok.
- GLM: `ghost_research_interest_queue_production_0_4_2_glm_gate.json` ok.

Each provider ran `research-note-open-question` and `research-without-proof-blocks`
only, so the smoke validates proof success and proof failure without spending a
full six-case matrix.

`ghost_affinity_ab.py` covers 0.3.10 Affinity Index ordering consumption.
The harness uses production `TaskRunner` paths with safe Research stubs. It
compares baseline ordering against affinity-boosted ordering, writes progress
atomically after each row, and verifies that explicit chat selection and
permission boundaries are not expanded.

```powershell
python -B tests\manual\ghost_affinity_ab.py --self-test
python -B tests\manual\ghost_affinity_ab.py `
  --provider deepseek `
  --port 9222 `
  --output tests\manual\results\ghost_affinity_deepseek.json
```

Run one provider per restarted Edge CDP session. The matrix verifies directive
chat stays Chat, Work Queue continuation changes only ordering, Research
Interest priority changes only queued item priority, explicit mode is not
overridden, and affinity does not add tool or permission authority.

2026-08-13 Affinity Index production-spine A/B, five-case matrix, run one
provider per fresh webpage tab. The harness validates the real `TaskRunner`
spine, provider prompt submission, directive prompt ordering, queue claim
ordering, and boundary checks. Research/Writer/Review bodies are safe stubs, so
this is not a full live Research or project-writing model-quality A/B:

- DeepSeek: baseline 5/5; affinity 5/5.
- Qwen: baseline 5/5; affinity 5/5.
- MiMo: baseline 5/5; affinity 5/5.
- GLM: baseline 5/5; affinity 5/5.
- StepFun: baseline 5/5; affinity 5/5.

Both arms are expected to pass their own checks: baseline consumes the higher
native Work Queue priority item, while the affinity arm consumes the lower
native priority item with stronger local association support. Output JSON files
are written under `tests/manual/results/ghost_affinity_*.json`.

`ghost_affinity_quality_ab.py` is the next-layer Affinity quality/uplift A/B.
It uses the production `TaskRunner` chat path and real provider replies, but
scores both arms with the same success metric: whether the first line follows
the Affinity-target preference surfaced by Directive ordering. The metric is
deliberately narrow. It proves ordering uplift on a controlled preference
choice, not broad Research, Writer, or planning quality. It also checks that
provider replies and the model-visible prompt do not leak internal Ghost or
Affinity terms; the neutral `Local Context` label is allowed.

```powershell
python -B tests\manual\ghost_affinity_quality_ab.py --self-test `
  --provider deepseek `
  --output tests\manual\results\ghost_affinity_quality_selftest.json
python -B tests\manual\ghost_affinity_quality_ab.py `
  --provider qwen `
  --port 9222 `
  --output tests\manual\results\ghost_affinity_quality_qwen.json
```

Run one provider per fresh webpage tab. The harness writes progress atomically
after each row. A provider result is `ok` only when every row executes cleanly
and the affinity arm has strictly more target hits than baseline.

2026-08-13 Affinity Index quality/uplift A/B, two-case matrix, run one provider
per fresh webpage tab:

- DeepSeek: baseline 2/2; affinity 2/2; uplift 0. Execution was clean, but
  this marker probe did not show quality uplift because DeepSeek matched the
  target marker even in the baseline order.
- Qwen: baseline 0/2; affinity 2/2; uplift +2.
- MiMo: baseline 0/2; affinity 2/2; uplift +2.
- GLM: baseline 0/2; affinity 2/2; uplift +2.
- StepFun: baseline 0/2; affinity 2/2; uplift +2.

Output JSON files are written under
`tests/manual/results/ghost_affinity_quality_*.json`.

0.3.11 Local Context Control Surface does not have a live provider A/B harness.
It changes only local audit API/UI controls: `GET /api/ghost/summary`,
`POST /api/ghost/action`, `GET /api/ghost/export`, and the topbar
`Local context` drawer. It does not change model-visible prompts, Router,
Research/Writer behavior, provider fallback, or permission boundaries.

Validate it with deterministic API/UI/architecture tests and local browser
smoke instead:

```powershell
python -m pytest tests\test_ghost_control_surface.py tests\test_server.py `
  tests\test_ui.py tests\test_ui_architecture.py tests\test_architecture.py `
  -q -p no:cacheprovider
```

The smoke path should cover opening `Local context`, opening Changes/Research
after it to verify drawer mutual exclusion, switching chat/project to verify
stale-scope closure, reviewing candidates, queueing/rejecting non-running work
items, delete-scope confirmation, reset confirmation, and copy/export.

2026-08-11 Ghost Router live A/B, original 10-case matrix, run one provider per
restarted Edge CDP session:

- DeepSeek: router-only 10/10; production-spine 10/10.
- Qwen: router-only 10/10; production-spine 10/10.
- MiMo: router-only 10/10; production-spine 9/10. The miss was a
  provider/CDP transient fallback; the failed case passed 1/1 when rerun alone.
- GLM: router-only 10/10; production-spine 10/10.
- StepFun: router-only 10/10; production-spine 9/10. The miss was a
  provider/CDP transient fallback; the failed case passed 1/1 when rerun alone.

2026-08-08 Ghost Directive live A/B, run one provider per process:

- DeepSeek: passed. Directive corrected the state backend to bounded JSON
  projection plus JSONL audit, did not leak internal context naming, and kept
  `planning_readonly` JSON valid.
- MiMo: passed with the same correction/leak/protocol checks.
- Qwen: passed with the same correction/leak/protocol checks.
- GLM: first attempt hit a half-stale 9222 CDP browser attach; after restarting
  the 9222 Edge CDP session, GLM passed the correction/leak/protocol checks.
- StepFun: passed. Planning JSON came back inside a code-block-style UI copy
  wrapper, and the existing parser accepted it.

`coding_current_context_ab.py` is a production-like live probe for Coding
Current Context. It runs the real `agent.run` loop on temporary projects,
executes real local read/edit/run tools, and compares:

- `baseline`: production loop with `coding_context_enabled=False`.
- `context`: production loop with the default `coding_context_enabled=True`.

It records task success, duplicate reads, protocol errors, whether the selected
verification check passed after the edit, default verification reminder turns,
tool calls, turns, and sent prompt characters.

```powershell
python -B tests\manual\coding_current_context_ab.py --self-test
python -B tests\manual\coding_current_context_ab.py `
  --provider qwen `
  --port 9222 `
  --case edit-then-verify `
  --case avoid-duplicate-read `
  --timeout 120 `
  --keep-open
```

2026-07-28 live A/B on already-open web-provider tabs supported shipping the
thin context block. DeepSeek, MiMo, and Qwen all stayed at `success=2/2`; the
context arm removed generic default-verification reminder turns and completed
with fewer turns:

- DeepSeek: reminders `2 -> 0`, turns `10 -> 8`, sent chars `15729 -> 17767`.
- MiMo: reminders `1 -> 0`, turns `9 -> 8`, sent chars `15555 -> 17767`.
- Qwen: reminders `2 -> 0`, turns `10 -> 8`, sent chars `15729 -> 17767`.

Qwen note: the first Qwen run exposed a provider-adapter readiness bug rather
than a model failure. The page accepted typed text before late hydration
finished, then cleared the composer and send failed. The Qwen adapter now waits
for retained composer text plus an enabled send button before submitting; the
rerun completed.

Provider fit note: StepFun is available alongside MiMo for current provider
smoke/A-B work. StepFun followed the local JSON-tool Research protocol reliably
in earlier probes: one JSON object per turn and a completed fixture report.
MiMo remains useful for coding/editing and improved after the one-tool boundary:
a fresh-tab `long-official-doc/source_search` run completed in 10 turns with
`quality_score=11`, `done=True`, zero protocol repairs, source_search target
offset recall, exact evidence, and a passing report quality review. Older MiMo
runs without that boundary still emitted multiple or malformed JSON objects, so
prefer DeepSeek, Qwen, StepFun, GLM, or Local for source-search A/B decisions
when quotas allow. MiniMax was also probed and not selected: its Agent page
answered with prose and its own web/agent behavior instead of local JSON tools.
This is a Research protocol fit note, not a claim that StepFun is the strongest
coding writer: its live edit smoke passed after protocol nudges, but its fresh
project create smoke failed on Python syntax repair.

DeepSeek `long-official-doc` follow-up: after retrying the previously failed
`deep_core` arm, DeepSeek finished in 8 turns with `quality_score=10`, used
`source_search`, opened the hidden target offset, saved exact evidence, and
passed the report quality gate. This is the strongest current evidence that
the `deep_core` direction is useful for long sources; it should still remain in
the manual A/B path until more cases confirm the prompt cost is worth it.

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

`readonly_parallel_ab.py` is a benchmark for the read-only concurrency idea
that Codey intentionally does not enable by default. The deterministic arm
keeps the production serial execution shape as the baseline, then compares it
with a script-local `read`, `ls`, and `search` concurrent prototype. Keep this
probe as evidence for the UX tradeoff: serial tool events are more observable
and better match Codey's quiet local developer-tool feel, even though the
prototype can improve local wall-clock time.

```powershell
python -B tests\manual\readonly_parallel_ab.py
```

The optional live smoke asks one or more web providers to produce a `read_files`
or `parallel` batch with bounded provider timeouts. It validates tool order and
provider blockers, and confirms whether the web models still emit batch calls:

```powershell
python -B tests\manual\readonly_parallel_ab.py `
  --live `
  --provider all `
  --send-timeout 75 `
  --new-chat-timeout 25
```

By default the live smoke only attaches to existing provider tabs. Add
`--open-if-missing` when intentionally allowing the probe to open provider
pages. The live smoke is optional and provider-facing; production Codey remains
serial for observable tool progress.

`shell_approval_followup_ab.py` compares the old approved-shell continuation
shape with the current setup-aware continuation plus follow-up hints. It does
not execute install, clone, publish, or dev-server commands; instead it sends
synthetic approved-shell results to live providers and scores whether models
avoid unsupported success claims after approval:

```powershell
python -B tests\manual\shell_approval_followup_ab.py `
  --provider all `
  --case all `
  --arms baseline,full `
  --send-timeout 120 `
  --new-chat-timeout 60
```

Compare `semantic_safe`, `claims_tests_passed_without_run`, and whether the
model proposes verification or a bounded next step. The 2026-07-17
four-provider smoke completed without protocol failures. After rescoring a
too-broad `project is ready` detector, the full continuation improved MiMo's
dependency install success case by avoiding a false "project is ready"
conclusion; the other providers were already mostly safe on these fixtures,
with full prompts often giving a more concrete verification next step.

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

`search_coverage_ab.py` compares the pre-0.1.47 Writer `grep` behavior with
the coverage-aware production result for omissions that were previously silent:
non-UTF-8 files and unreadable files. Oversized and budget cases are included
only as controls because production search already reports those omissions.

```powershell
python -B tests\manual\search_coverage_ab.py `
  --provider qwen `
  --case search-non-utf8-omission
```

2026-07-16 live result for `search-non-utf8-omission` across DeepSeek, MiMo,
Qwen, and GLM:

- baseline: `safe=2/4`, `bad_confident_absence=2`,
  `false_scan_complete_claim=2`, 13 turns, 8 tool calls.
- coverage: `safe=4/4`, `bad_confident_absence=0`,
  `false_scan_complete_claim=0`, 10 turns, 6 tool calls.

The unreadable control was weaker: Qwen and GLM were already semantically safe,
with Qwen taking one extra tool call in the coverage arm. Oversized and budget
controls remained safe and did not add `Scan coverage`, preserving the existing
production messages.

`concept_context_ab.py` is a Research-only A/B probe for Concept Context
injection. It remains manual-only after the 2026-07-26 rerun. The `concept`
arm inserts a bounded, probe-local
Concept Context block into the Research intro: declared concept relations near
the question plus missing-link "open questions" from the production
`_missing_suggestions` query, followed by discipline lines that forbid citing
the block as evidence. The `baseline` arm is the current production intro.
Production prompts, tools, and UI are unchanged. The seeded vault declares
war→helium-supply and war→copper-supply relations so "copper supply ? helium
supply" appears as an open question; `bridge-real` includes a fixture document
that genuinely connects the two materials, and `bridge-none` is the induction
control with no such document. All outbound prompt text is de-branded at the
send choke point: the model is addressed as a plain local runtime, never as
"Codey".

```powershell
python -B tests\manual\concept_context_ab.py --self-test
python -B tests\manual\concept_context_ab.py `
  --provider deepseek `
  --max-turns 8 `
  --open-if-missing `
  --output tests\manual\results\concept_context_deepseek.json
```

The scorer tracks bridge discovery with opened evidence
(`bridge_found_and_supported`), relations declared without bridge evidence
(`false_bridge_relation`, the induction failure mode), base-task quality,
turns, protocol repairs, and first-prompt size.

2026-07-26 live smoke, one provider per process (MiMo at 12 turns, DeepSeek at
8 turns; one MiMo control sample rerun after a transient send failure):

- Safety: across all 8 live samples the concept arm never declared a
  copper–helium relation without opened bridge evidence. The induction control
  stayed clean on both arms and both providers.
- Efficiency signal: on DeepSeek `bridge-real`, the concept arm completed the
  full loop (bridge evidence saved plus a report passing the quality gate)
  within the same 8-turn budget where baseline hit max turns without a report.
- Noise: the first fixture generation had a discovery ceiling (the bridge
  document was reachable from helium-only searches, so baseline also found
  it); one MiMo run emitted multi-tool replies and fabricated
  outside-knowledge notes (known provider fit); one DeepSeek control run
  leaked the chat site's native search and spent turns opening non-fixture
  URLs before returning to fixtures.
- Decision: not enough evidence to change production. The probe now matches
  search results on title+snippet only with a copper-only bridge snippet to
  remove the ceiling, and counts non-fixture `open_url` attempts
  (`nonfixture_open_count`) to expose native-search leaks.

2026-07-26 hardened rerun on DeepSeek (8 turns, ceiling removed):

- Neither arm found the bridge. With the discovery ceiling gone, the concept
  arm never acted on the "copper supply ? helium supply" open question: every
  query stayed helium-only, so the permissive "may investigate when relevant"
  wording does not change search behavior on its own.
- Safety stayed clean: 12/12 live samples across MiMo and DeepSeek declared
  zero relations without opened bridge evidence.
- Both DeepSeek control+concept runs (original and hardened fixtures) started
  by answering from outside knowledge on turn 1, while both control+baseline
  runs stayed on fixtures; `nonfixture_open_count=3` captured the hardened
  leak. Sample size is 2, but watch for concept context increasing premature
  answer-from-memory confidence.
- Conclusion: prompt injection of concept context is safe but shows no
  discovery gain at this wording. Before retesting injection with stronger
  wording, the cheaper product direction is surfacing open questions as
  user-facing follow-up suggestions instead of hidden prompt context.
