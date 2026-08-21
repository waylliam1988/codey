# Codey Test Report

## 0.4.4 Bounded Research Planner v1 (implementation draft)

Codey 0.4.4 moves Research orchestration into `ResearchPipeline` and adds the
first bounded planner execution loop while keeping provider/session/UI
ownership outside the Research lifecycle. This draft also includes the Qwen
homepage submit-readiness repair that was found while running the 0.4.4 bounded
planner web-provider A/B.

Production changes under validation:

- `ResearchPipeline` owns initial research, proof review, planner execution,
  follow-up synthesis, final proof review, and the final Evidence Ledger write.
  `TaskRunner` keeps the outer provider/session/trace/mode lifecycle.
- `ResearchIterationRun` is the explicit single-iteration primitive used by the
  Pipeline and test harnesses; runtime tools are passed across iteration
  boundaries instead of being hidden on the result object.
- Follow-up metadata is surfaced above the raw `ResearchRunResult`, including
  `followup_applied`, `followup_rounds`, and `planner_stop_reason`.
- The manual bounded planner A/B harness records atomic send/reply traces and
  conservative paired `followup_usefulness` summaries. The planner arm now
  injects the production `run_evidence_followup()` path and production
  deterministic merge; the remaining A/B-only behavior is limited to the
  fixture material-phase executor that exposes hidden source B for controlled
  comparison.
- Qwen Studio homepage first submit now waits out a short false-ready state:
  the page can expose `textarea.message-input-textarea` and
  `button.send-button` before its homepage submit handler is hydrated. The wait
  is scoped only to the Qwen home URL and is capped by the same provider
  timeout budget.

Validation during implementation:

```text
python -B -m pytest tests\test_qwen.py -q
# 57 passed in 1.39s

python -B tests\manual\qwen_submit_probe.py --timeout 60 'Reply exactly {"ok":true} and no markdown.'
# Qwen new_chat seconds=4.53; send seconds=6.17; reply={"ok":true}

ad hoc Qwen provider.new_chat(timeout=60) live probe
# new_chat_ok 4.517 https://chat.qwen.ai/

python -B -m pytest
# 2251 passed in 386.30s

python -B -m py_compile tests\manual\bounded_research_planner_ab.py
# passed

python -B tests\manual\bounded_research_planner_ab.py --self-test
# self-test ok
```

The 2026-08-21 trace replay check fed the five successful `evidenceonly3`
follow-up replies back into the current production `run_evidence_followup()`.
DeepSeek, MiMo, Qwen, StepFun, and GLM all accepted the strict explicit
`{"tool":"knowledge_write","args":{...}}` shape and wrote exactly one new
evidence item. This shows the later schema hardening does not break those
successful follow-up replies, while the live A/B harness now measures the
production follow-up prompt rather than the older harness-only
`iteration_context` controller.

Live bounded-planner A/B evidence is recorded under `tests/manual/results/` and
summarized in `tests/manual/bounded_research_planner_ab_reports.md`. The current
post-production paired `widget_noop` checks exercise
`ab_followup_mode=production_evidence_followup`:

- DeepSeek production path: score `5 -> 6`, useful, one new
  evidence-backed source, coverage `0.556 -> 0.667`,
  unsupported-claim rate unchanged at `0.750`, provider sends `5 -> 6`,
  elapsed time `+3.984s`.
- Qwen production path: score `5 -> 6`, one new evidence-backed source,
  coverage `0.556 -> 0.667`, provider sends `5 -> 6`, elapsed time
  `+8.974s`, but `followup_usefulness=false` because unsupported-claim rate
  regressed from `0.333` to `0.750`.
- StepFun production path: score stayed `1 -> 1`, final source/evidence count
  stayed `1/1`, provider sends `6 -> 8`, elapsed time `+28.387s`, and
  `followup_usefulness=false`. The planner material phase fetched hidden source
  B, but the run stayed protocol/not-answered and stopped at
  `candidate_not_selected` with no final material gain.

The earlier pre-integration `evidenceonly3` paired web-provider results:

- DeepSeek: score `5 -> 6`, useful, one new evidence-backed source, coverage
  unchanged at `0.556`, unsupported-claim rate `-0.333`, provider sends `+3`.
- MiMo: score `5 -> 6`, useful, one new evidence-backed source, coverage
  `+0.112`, unsupported-claim rate `-0.333`, provider sends `+3`.
- Qwen: score `5 -> 6`, useful after the homepage readiness fix, one new
  evidence-backed source, coverage unchanged at `0.667`, unsupported-claim
  rate `-0.333`, provider sends `+1`.
- GLM evidence-only3: score `1 -> 6`, useful, one new evidence-backed source,
  coverage `+0.112`, unsupported-claim rate unchanged at `0.000`, provider
  sends `+1`.
- StepFun evidence-only3: score `1 -> 6`, useful, one new evidence-backed
  source, coverage `+0.112`, unsupported-claim rate unchanged at `0.000`,
  provider sends `+1`.

2026-08-21 evidence-only3 rerun artifacts:

```text
python -B tests\manual\bounded_research_planner_ab.py --provider deepseek --case widget_noop --arms baseline --open-if-missing --output tests\manual\results\bounded_research_planner_ab-deepseek-evidenceonly3-paired-widget-20260821.json
python -B tests\manual\bounded_research_planner_ab.py --provider deepseek --case widget_noop --arms planner --open-if-missing --output tests\manual\results\bounded_research_planner_ab-deepseek-evidenceonly3-paired-widget-20260821.json
# DeepSeek: score 5 -> 6, useful=true

python -B tests\manual\bounded_research_planner_ab.py --provider mimo --case widget_noop --arms baseline --open-if-missing --output tests\manual\results\bounded_research_planner_ab-mimo-evidenceonly3-paired-widget-20260821.json
python -B tests\manual\bounded_research_planner_ab.py --provider mimo --case widget_noop --arms planner --open-if-missing --output tests\manual\results\bounded_research_planner_ab-mimo-evidenceonly3-paired-widget-20260821.json
# MiMo: score 5 -> 6, useful=true

python -B tests\manual\bounded_research_planner_ab.py --provider qwen --case widget_noop --arms baseline --open-if-missing --output tests\manual\results\bounded_research_planner_ab-qwen-evidenceonly3-paired-widget-20260821.json
python -B tests\manual\bounded_research_planner_ab.py --provider qwen --case widget_noop --arms planner --open-if-missing --output tests\manual\results\bounded_research_planner_ab-qwen-evidenceonly3-paired-widget-20260821.json
# Qwen: score 5 -> 6, useful=true

python -B tests\manual\bounded_research_planner_ab.py --provider deepseek --case widget_noop --arms baseline --open-if-missing --output tests\manual\results\bounded_research_planner_ab-deepseek-production-20260821.json
python -B tests\manual\bounded_research_planner_ab.py --provider deepseek --case widget_noop --arms planner --open-if-missing --output tests\manual\results\bounded_research_planner_ab-deepseek-production-20260821.json
# DeepSeek production path: score 5 -> 6, useful=true

python -B tests\manual\bounded_research_planner_ab.py --provider qwen --case widget_noop --arms baseline --open-if-missing --output tests\manual\results\bounded_research_planner_ab-qwen-production-20260821.json
python -B tests\manual\bounded_research_planner_ab.py --provider qwen --case widget_noop --arms planner --open-if-missing --output tests\manual\results\bounded_research_planner_ab-qwen-production-20260821.json
# Qwen production path: score 5 -> 6, useful=false due unsupported-rate regression

python -B tests\manual\bounded_research_planner_ab.py --provider stepfun --case widget_noop --arms baseline --open-if-missing --output tests\manual\results\bounded_research_planner_ab-stepfun-production-20260821.json
python -B tests\manual\bounded_research_planner_ab.py --provider stepfun --case widget_noop --arms planner --open-if-missing --output tests\manual\results\bounded_research_planner_ab-stepfun-production-20260821.json
# StepFun production path: score 1 -> 1, useful=false; material fetched, candidate not selected

python -B tests\manual\bounded_research_merge_projection.py --self-test
# self-test ok

python -B tests\manual\bounded_research_merge_projection.py --output tests\manual\results\bounded_research_merge_projection-20260821.json
# Projection kept five evidence-only3 rows useful and converted Qwen production
# plus the earlier StepFun production row to useful.

python -B tests\manual\bounded_research_planner_ab.py --provider stepfun --case widget_noop --arms baseline --open-if-missing --output tests\manual\results\bounded_research_planner_ab-stepfun-production-narrowmerge-20260821.json
python -B tests\manual\bounded_research_planner_ab.py --provider stepfun --case widget_noop --arms planner --open-if-missing --output tests\manual\results\bounded_research_planner_ab-stepfun-production-narrowmerge-20260821.json
python -B tests\manual\bounded_research_merge_projection.py --input tests\manual\results\bounded_research_planner_ab-stepfun-production-narrowmerge-20260821.json --output tests\manual\results\bounded_research_merge_projection-stepfun-narrowmerge-20260821.json
# Fresh StepFun rerun: raw score 5 -> 1, stop=no_tool_calls; projection stayed
# 1/false because no fresh evidence-only reply existed. This row was collected
# while StepFun was rate-limited after repeated tests, so it is an invalid gate
# sample rather than a planner/merge regression.

python -B tests\manual\bounded_research_planner_ab.py --provider stepfun --case widget_noop --arms planner --open-if-missing --output tests\manual\results\bounded_research_planner_ab-stepfun-production-rerun2-20260821.json
python -B tests\manual\bounded_research_planner_ab.py --provider stepfun --case widget_noop --arms baseline --open-if-missing --output tests\manual\results\bounded_research_planner_ab-stepfun-production-rerun2-20260821.json
python -B tests\manual\bounded_research_merge_projection.py --input tests\manual\results\bounded_research_planner_ab-stepfun-production-rerun2-20260821.json --output tests\manual\results\bounded_research_merge_projection-stepfun-rerun2-20260821.json
# Clean StepFun paired rerun: raw production stayed 1/false with
# candidate_not_selected despite attempted_fresh_source_count=1 and
# attempted_new_evidence_count=1; projection converted it to 6/true. This
# validates the narrow evidence-backed record_merge production fix.

python -B -m py_compile codey\research\record_merge.py tests\test_research_record_merge.py tests\test_research_pipeline.py tests\manual\bounded_research_merge_projection.py
# ok

ruff check codey\research\record_merge.py tests\test_research_record_merge.py tests\test_research_pipeline.py tests\manual\bounded_research_merge_projection.py
# All checks passed

pytest tests\test_research_evidence_followup.py tests\test_research_plan_executor.py tests\test_research_record_merge.py tests\test_research_pipeline.py tests\test_run_trace.py tests\test_task_runner_run_trace.py -q
# 66 passed

pytest -q
# 2264 passed, 9 skipped, 638 subtests passed in 368.57s
```

## 0.4.3 Source Connector Boundary + Query Planner Dry Run v1

Codey 0.4.3 adds the Research source boundary, deterministic planner dry-run,
and the connector-aware PubMed/arXiv Research search path. It lets Codey
describe which source types should be checked next and can surface PubMed/arXiv
hits through the normal Research search flow without automatic recursive
follow-up planning.

Production changes:

- New `codey/research/source_connectors.py` defines `SourceConnectorSpec`,
  `SourceConnectorRegistry`, `SourceHit`, `FetchedSource`, and
  `SourceConnectorResult`. The built-in registry ships fixture/local coverage
  for `local_file`, `csv_tsv`, `json_file`, `arxiv`, and `pubmed`; `openalex`
  is deferred and `rss` is optional, so neither counts as shipped.
- Recorded fixtures now cover local text, CSV, TSV, JSON, arXiv Atom, and
  PubMed XML under `tests/fixtures/research_connectors/`. Fixture hits produce
  stable source/hit refs. Local reads are confined to explicit allowed roots,
  CSV/TSV parsing uses Python's `csv` module, and recorded URL hits still pass
  the Research URL guard before fixture fetch.
- New `codey/research/query_planner.py` builds a bounded `ResearchPlan`
  dry-run from proof-review gaps and connector metadata. It prefers PubMed for
  medical/life-science questions, arXiv for paper/preprint/ML-style questions,
  and local connectors for file/table/JSON questions.
- Run Trace now stores bounded `research_plans` summaries: plan ref, question
  digest, proof ref, query count, source preference ids, max bounds, warnings,
  and reason codes. It does not store query text, raw prompts, source text,
  fetched pages, raw URLs, or raw absolute paths. The trace also records
  model-visible controller action and compiled runtime tool contract hashes as
  separate audit fields. Non-collection list fields are ignored instead of
  iterating strings or raising on `None`.
- The Capability Registry and Event / Capability Matrix declare
  `research_source_connectors`, `research_connector_search`, and
  `research_query_planner`. Architecture tests keep connector/planner modules
  away from provider adapters, browser code, tool runtime, server/TaskRunner
  runtime layers, Ghost runtime, subprocess, and plugin loaders.
- PubMed/arXiv connector hits now enter Research search by default through
  `ConnectorAwareSearchProvider`. They remain locator candidates until opened;
  only fetched/opened sources can become evidence in the ledger.
- Live PubMed/arXiv connector queries are built from the shared safe term
  boundary used by the dry-run planner; raw secrets and secret marker/value
  windows such as `api key ...`, `api key is ...`, `password is ...`,
  `password is equal to ...`, `password is set to ...`,
  `password is configured as ...`, `api key called ...`, `api key named ...`,
  `client secret known as ...`, over-padded or punctuation-separated connector
  phrases such as `password is configured as known as called ...` and
  `password - is - configured - as - known - as - called - ...`, Chinese
  windows such as `密码 是 ...` and `密钥等于 ...`, `private key is ...`,
  `client_secret=...`, and
  `Authorization: Bearer ...`, plus `access_token ...`, `passphrase ...`, and
  value-shaped contextual followers such as `token abcdef`, `cookie abcdef`,
  or `jwt abcdef`, are masked before planner previews, connector digests, and
  any source API request. Multi-word values after explicit secret markers are
  bounded by domain terms, while NLP/security queries such as
  `token classification benchmark` keep their domain terms. Cleaned domain
  terms can still drive connectors; URLs and local paths are dropped, connector
  lookup is skipped only when no safe terms remain, and live connector
  routing/request assembly reuse the same `SafeConnectorQuery`. Browser
  fallback search starts before connector lookup, connector lookup has a short
  global request budget, direct PubMed/arXiv URL fetch failures fall back to
  browser fetch, and normal browser result de-duplication keeps
  query-string-distinct URLs.
- Recorded PubMed/arXiv fixture parsers and recorded fetches validate both
  connector-specific host and source-ID shape. `SourceHit` metadata refs and
  scalar audit fields filter secret-looking values, `FetchedSource` scalar audit
  fields are allow-listed, connector catalog id/kind values reject
  secret-looking or non-canonical codes, catalog hints and result warning/error
  payloads filter secret-looking codes, connector result query digests use
  sanitized terms, and proof-complete no-op plans carry no availability-warning
  noise.
- Run Trace records bounded connector fallback error summaries as connector,
  action, error kind, and count only; raw URLs, queries, exception messages, and
  secret-looking values are excluded. Research plan, evidence-ledger, and
  proof-review trace sinks share the same bounded list handling and
  secret-looking reason/warning filtering without dropping safe audit codes such
  as `token_budget_exceeded` or `authorization_required`. Live connector API
  transport uses a neutral tool name and User-Agent without the product name.
- The Research controller no longer exposes overloaded
  `open_url(result_id/source_id/hit_id)` shapes to models. It exposes distinct
  `open_result`, `reopen_source`, and `open_hit` actions, then compiles those
  actions to the runtime `open_url` execution path.
- Browser-backed Research search explicitly reuses one dedicated Research
  profile/port for ordinary runs. Direct `BrowserSearchProvider()` construction
  remains isolated by default, and browser attach/port waits are bounded at 20
  seconds for faster failure feedback. Task cancellation bypasses isolated CDP
  launch retries and browser search page navigation retries.
- Model-visible Research prompt, repair, controller, and tool-result surfaces no
  longer name the product when giving protocol instructions or evidence
  fallback warnings; provider-specific transport quirks, such as GLM's
  typographic JSON quotes, stay in the provider adapter instead of the generic
  Research protocol parser. The Research fallback contract no longer carries a
  tool or argument alias layer; legacy names such as `open`, `fetch`,
  `queries`, and `done.summary` fail through the typed protocol path. That can
  cost one repair turn for providers that still emit legacy names; the clean
  boundary is provider adapter normalization plus typed repair prompts, not
  shared parser compatibility. Hidden `name` tool fields, top-level argument
  fields, extra top-level fields, and extra JSON objects are also rejected;
  exactly one JSON object with only top-level `tool` plus `args` is the accepted
  Research JSON shape.
- Planner and live connector routing share one domain vocabulary, including
  genetic/genomic and RAG/NLP/retrieval/benchmark terms. Registry
  availability/capability flags are enforced by the live wrapper; connector
  deadlines are strict, safe scientific slash terms are preserved, path-like
  slash tokens including CamelCase path shapes are rejected, and shared
  redaction helpers split marker words from key-shaped values so ordinary words
  such as `secreted` and `secretion` remain searchable.
- `codey/research/done_finalizer.py` now runs before the Research
  report-quality gate as a narrow citation compiler. It compiles reliable
  source-id/contextual refs and parsed numeric source rows, renders the final
  source table from opened sources that have saved evidence excerpts, removes
  opened-only sources, and leaves unsupported claims for the quality gate
  rather than adding citations. Numeric refs and source-id refs are bound
  separately, duplicate old numbers with conflicting URLs are rejected, and
  unambiguous single-source numeric drift can still be normalized. The quality
  gate checks pre-heading prose, report body text, no-citable reports, and the
  source section for internal source-id leaks. The source section check is
  line-level: parsed source rows protect source titles such as `[S1]`, while
  separate notes and contextual leaks such as `source_id=s9` remain blockers.
- `codey/research/citation_scanner.py` now holds the shared citation and
  source-id scanners, and `review_report_quality()` is split into small review
  helpers for missing sections, source-id leaks, no-citable reports,
  provenance, source-table validation, body citation checks, and
  source-quality warnings.
- `tools/ui_e2e.py` now treats screenshot capture as best effort in CI so the
  real Edge smoke records a fallback text artifact instead of failing the whole
  run on a screenshot timeout.
- Qwen waits only for an interactive, non-generating composer before filling,
  retries only the input-fill phase when hydration clears the draft, rejects a
  lost message before clicking, and never repeats a whole send because
  post-click response confirmation is slow. Browser PDF transport also uses a
  neutral User-Agent.

Validation during implementation:

```text
python -m py_compile codey\research\source_connectors.py codey\research\protocols.py codey\research\controller.py tests\test_source_connectors.py tests\test_connector_search.py tests\test_research_protocol_contract.py tests\test_research_controller.py
# passed

python -m pytest tests\test_source_connectors.py tests\test_connector_search.py tests\test_query_planner.py tests\test_research_protocol_contract.py tests\test_research_controller.py tests\test_research.py tests\test_glm.py -q
# 231 passed, 7 subtests passed in 14.37s

python -m pytest tests\test_source_connectors.py tests\test_connector_search.py tests\test_query_planner.py tests\test_browser.py tests\test_research.py tests\test_run_trace.py tests\test_architecture.py -q
# 228 passed, 122 subtests passed in 14.46s

ruff check codey tests
# All checks passed!

python -m compileall -q codey tests
# passed

python -m pytest -q
# 2191 passed, 9 skipped, 621 subtests passed in 410.40s (0:06:50)

python -m pytest tests\test_source_connectors.py tests\test_query_planner.py tests\test_run_trace.py tests\test_task_runner_run_trace.py tests\test_capabilities.py tests\test_event_matrix.py tests\test_architecture.py tests\test_server.py::WebAssetTests::test_runtime_version_matches_release_docs -q -p no:cacheprovider
# 91 passed, 308 subtests passed

python -m pytest tests\test_glm.py tests\test_research_protocol_contract.py tests\test_research_controller.py -q
# 73 passed

python -m pytest tests\test_browser.py -q -p no:cacheprovider
# 49 passed

python -B tests\manual\source_connector_ab.py --self-test
# self-test ok

python -m pytest tests\test_source_connectors.py tests\test_query_planner.py tests\test_connector_search.py tests\test_research_controller.py tests\test_research.py tests\test_server.py tests\test_run_trace.py tests\test_capabilities.py tests\test_architecture.py tests\test_browser.py tests\test_qwen.py tests\test_glm.py -q -p no:cacheprovider
# 505 passed, 1 skipped, 121 subtests passed in 169.07s (0:02:49)

ruff check codey tests
# All checks passed!

python -m compileall codey tests
# passed

python -m pytest -q -p no:cacheprovider
# 2154 passed, 9 skipped, 619 subtests passed in 368.31s (0:06:08)

python -m pytest tests\test_source_connectors.py tests\test_research_proof_quality.py tests\test_research.py tests\test_browser.py tests\test_qwen.py -q
# 222 passed, 7 subtests passed in 13.88s

python -m pytest -q
# 2160 passed, 9 skipped, 619 subtests passed in 449.47s (0:07:29)
```

Live provider A/B is recorded under `tests/manual/results/` with atomic row
writes so failed or missing rows can be resumed without rerunning completed
provider traffic. The connector path is promoted as normal 0.4.3 behavior
because the smoke showed source-selection value without breaking the controller
contract: DeepSeek improved PubMed targeting, MiMo and StepFun connector arms
opened PubMed target hosts, Qwen improved on arXiv after the provenance fix, and
DeepSeek/Qwen/MiMo/StepFun/GLM all reached arXiv target hosts in at least one
recorded arm. The live evidence is connector smoke, not a proof-quality win
claim: several providers still stopped at `max_turns` or protocol repair, and
GLM PubMed rerun was paused after repeated attempts hit provider rate limits.

MiMo done-stage A/B evidence from 2026-08-19 is archived under
`tests/manual/results/`. The baseline row came from
`source_connector_done_ab-mimo-pubmed-max24.json`; the cleaner pre-production
finalizer sample was rerun alone after the baseline process finished and is stored in
`source_connector_done_ab-mimo-pubmed-finalizer-only.json`. Full model report
text for both arms is archived in
`tests/manual/source_connector_done_ab_mimo_pubmed_reports.md`.

```text
python tests/manual/source_connector_done_ab.py --provider mimo --case pubmed --arms baseline,finalizer --samples 1 --open-if-missing --output tests/manual/results/source_connector_done_ab-mimo-pubmed-max24.json --trace-output tests/manual/results/source_connector_done_ab-mimo-pubmed-max24.trace.json
# baseline row: score=9, connector_valid=true, done_attempts=2,
# quality_retry_count=1, first_done_passed=false, eventual_done_passed=true

python tests/manual/source_connector_done_ab.py --provider mimo --case pubmed --arms finalizer --samples 1 --open-if-missing --output tests/manual/results/source_connector_done_ab-mimo-pubmed-finalizer-only.json --trace-output tests/manual/results/source_connector_done_ab-mimo-pubmed-finalizer-only.trace.json
# finalizer-only row: score=9, connector_valid=true, done_attempts=1,
# quality_retry_count=0, first_done_passed=true, eventual_done_passed=true,
# finalizer_rewrites=1
```

The paired same-process run also completed the finalizer arm in one done
attempt, but that row recorded browser-search connector errors. The cleaner
single-arm rerun removed those connector errors while preserving the done-stage
gain. This supports the finalizer direction for citation/source formatting:
Mimo still produced proof-quality gaps, but the final report passed the
report-quality gate on the first `done` call when source numbering was compiled
from saved evidence.

That same narrow citation compiler is now wired into production `done`
handling before the quality gate. It standardizes source numbering and the
`来源` table for reliable source-id or parsed source-map references, but it
does not invent citations, rebind unmapped numeric references, remap source-id
references through stale numeric source tables, rewrite non-citation bracket
text such as `[2nd]`, leak unresolved internal source IDs, or bypass blocker
checks. It still repairs numeric drift when parsed old source rows all dedupe
to the same canonical URL, such as a body `[1]` with parsed source row `[10]`
URL, and also repairs repeated numeric labels for the same source. It leaves
ambiguous multi-source drift for the quality repair loop. Source-id
leakage checks are shared between the compiler and quality gate and cover
pre-heading prose plus the report body. The `## 来源` section is scanned line
by line: parsed source rows protect titles such as
`Analysis of [S1] Subunit Protein`, while free-text notes such as `note [s9]`
and contextual leaks such as `source_id=s9` are treated as internal IDs.
Conflicting duplicate old source numbers are rejected. When a
report has no citable sources, the compiler re-renders the sectioned report and
drops any preamble before handing the result to the quality gate. The manual
`finalizer` arm used in these historical rows has been removed from the current
probe because production now runs the compiler for every arm.

Post-merge verification for the production citation compiler:

```text
python -m py_compile codey/research/report_quality.py codey/research/done_finalizer.py codey/research/runner.py codey/run_trace.py tests/test_research.py tests/test_run_trace.py tests/manual/source_connector_done_ab.py
# passed

python -m ruff check codey/research/report_quality.py codey/research/done_finalizer.py tests/test_research.py
# All checks passed!

python -m pytest tests/test_research.py -k "source_id or no_citable or duplicate_source_numbers or done_finalizer or provenance"
# 34 passed, 88 deselected

python -m pytest tests/test_run_trace.py -k "done_compilation or connector_errors"
# 2 passed

python -B tests/manual/source_connector_done_ab.py --self-test
# self-test ok

python -m pytest tests/test_research.py tests/test_run_trace.py
# 149 passed in 13.17s

python -m pytest
# 2232 passed, 9 skipped in 428.92s (0:07:08)
```

Qwen done-stage A/B evidence from 2026-08-19 is archived under
`tests/manual/results/`. The baseline row came from
`source_connector_done_ab-qwen-pubmed-baseline-20260819-134023.json`; the
cleaner pre-production finalizer sample was rerun alone after the baseline process finished
and is stored in
`source_connector_done_ab-qwen-pubmed-finalizer-20260819-134023.json`. Full
model report text for both arms is archived in
`tests/manual/source_connector_done_ab_qwen_pubmed_reports.md`.

```text
python tests/manual/source_connector_done_ab.py --provider qwen --case pubmed --arms baseline --samples 1 --open-if-missing --output tests/manual/results/source_connector_done_ab-qwen-pubmed-baseline-20260819-134023.json --trace-output tests/manual/results/source_connector_done_ab-qwen-pubmed-baseline-20260819-134023.trace.json
# baseline row: score=5, connector_valid=false, done_attempts=2,
# quality_retry_count=1, first_done_passed=false, eventual_done_passed=true

python tests/manual/source_connector_done_ab.py --provider qwen --case pubmed --arms finalizer --samples 1 --open-if-missing --output tests/manual/results/source_connector_done_ab-qwen-pubmed-finalizer-20260819-134023.json --trace-output tests/manual/results/source_connector_done_ab-qwen-pubmed-finalizer-20260819-134023.trace.json
# finalizer row: score=5, connector_valid=false, done_attempts=1,
# quality_retry_count=0, first_done_passed=true, eventual_done_passed=true,
# finalizer_rewrites=1
```

Unlike MiMo, Qwen's clean finalizer run did not recover connector validity or
target-host selection. It did, however, cut one done attempt and one quality
retry, so the prompt overlay still appears useful for report closure even when
connector selection stays weak.

## 0.4.2 Research Proof Quality Gate + Planner Signals v0

Codey 0.4.2 adds a deterministic Research proof-quality gate for queued
Research/open-question work items. It upgrades completion from "a research
artifact exists" to "the queued question is covered by cited, opened-source,
locator-backed, support-relation evidence." The gate also emits planner signals
for later bounded follow-up search, but it does not execute those plans.

Production changes:

- New `codey/research/proof_quality.py` reviews `ResearchRecord` objects and
  the durable Evidence Ledger read model without model calls or source fetches.
  It checks answer coverage, citations, evidence refs, locator/source
  consistency, support relation direction, assumptions, counter/limitations,
  source-trust warnings, overclaim warnings, and missing-evidence reason codes.
- New `codey/research/completion_gate.py` gives TaskRunner a narrow Research
  queue completion boundary. Queued `research` / `open_question` items complete
  only when the proof review passes and yields `research_proof:<16 hex>`.
  Ordinary manual Research is not blocked by the queue gate.
- `ghost/work_queue.py` stays independent of Research runtime modules. It only
  validates the generated-looking `research_proof:<16 hex>` primary proof shape
  for research/open-question completion. Legacy `research:*` refs may remain as
  auxiliary refs but cannot complete new queued Research by themselves.
- Run Trace now stores only bounded `research_proof_reviews` summaries:
  proof ref, queued-question digest, booleans, answer coverage score, counts,
  reason codes, and record id/digest when a record exists. Missing-record gate
  blocks still leave an auditable proof review without storing the queued
  question text. Passing proof reviews must include valid record id/digest, and
  duplicate proof summaries are de-duplicated by proof/question/reason identity.
  The trace does not store planner signal text, raw prompts, raw model
  responses, raw URLs, raw paths, source text, or fetched pages.
- The Capability Registry and Event / Capability Matrix declare
  `research_proof_quality` as a deterministic completion gate and planner
  signal producer. Architecture tests keep proof modules away from provider
  adapters, browser code, tool runtime, server/TaskRunner runtime layers,
  Ghost runtime, and plugin loaders.

Validation during implementation:

```text
python -m pytest tests\test_run_trace.py tests\test_task_runner_work_queue.py tests\test_research_completion_gate.py tests\test_research_proof_quality.py -q -p no:cacheprovider
# 43 passed

python -m pytest tests\test_research_proof_quality.py tests\test_research_completion_gate.py tests\test_ghost_work_queue.py tests\test_task_runner_work_queue.py tests\test_run_trace.py tests\test_capabilities.py -q -p no:cacheprovider
# 92 passed

python -m pytest tests\test_task_runner_affinity.py tests\test_task_runner_router.py tests\test_task_runner_work_queue.py tests\test_ghost_work_queue.py tests\test_research_interest_queue.py tests\test_ghost_work_queue_ab.py -q -p no:cacheprovider
# 72 passed

python -B tests\manual\ghost_research_interest_queue_production_ab.py --self-test
# self-test ok

python -B tests\manual\ghost_work_queue_production_ab.py --self-test
# self-test ok

python -m pytest tests\test_research_proof_quality.py tests\test_research_completion_gate.py tests\test_research_identity.py tests\test_research_evidence_ledger.py tests\test_research_object_model.py tests\test_research.py tests\test_ghost_work_queue.py tests\test_research_interest_queue.py tests\test_task_runner_work_queue.py tests\test_task_runner_affinity.py tests\test_task_runner_router.py tests\test_run_trace.py tests\test_task_runner_run_trace.py tests\test_capabilities.py tests\test_event_matrix.py tests\test_architecture.py tests\test_server.py::WebAssetTests::test_runtime_version_matches_release_docs -q -p no:cacheprovider
# 279 passed, 294 subtests passed

python -m pytest -q -p no:cacheprovider
# 2088 passed, 9 skipped, 587 subtests passed in 368.50s (0:06:08)

python -m pytest tests\test_event_matrix.py tests\test_capabilities.py tests\test_server.py::WebAssetTests::test_runtime_version_matches_release_docs -q -p no:cacheprovider
# 28 passed, 190 subtests passed

python -m ruff check codey tests --no-cache
# All checks passed!

python -m compileall codey tests
# passed

git diff --check
# passed
```

Small Research/Ghost queue A/B:

```text
python -B tests\manual\ghost_work_queue_production_ab.py --provider <provider> --case research-item --output tests\manual\results\ghost_work_queue_production_0_4_2_<provider>_research_item.json
python -B tests\manual\ghost_research_interest_queue_production_ab.py --provider <provider> --case research-note-open-question --case research-without-proof-blocks --output tests\manual\results\ghost_research_interest_queue_production_0_4_2_<provider>_gate.json
```

2026-08-17 one-provider-at-a-time live result against Edge CDP 9222:

- DeepSeek: work queue `research-item` ok; research-interest proof/no-proof gate ok.
- Qwen: work queue `research-item` ok; research-interest proof/no-proof gate ok.
- MiMo: work queue `research-item` ok; research-interest proof/no-proof gate ok.
- StepFun: work queue `research-item` ok; research-interest proof/no-proof gate ok.
- GLM: work queue `research-item` ok; research-interest proof/no-proof gate ok.

The first DeepSeek attempt failed before TaskRunner because CDP port 9222 was
not open. After the browser CDP session came up, DeepSeek was rerun as a single
provider and passed. This release does not need a broad provider/prompt A/B:
Research prompts, tool schemas, model-visible tool-result wording, Router
behavior, provider fallback ordering, permissions, UI, task receipts, and SSE
payload shape are unchanged.

## 0.4.1 Evidence Ledger v2

Codey 0.4.1 adds the durable Evidence Ledger v2 read model. Every completed
Research run can now append its bounded `ResearchRecord` into a local
session/project-scoped evidence ledger, giving later proof-quality checks a
stable source/evidence/claim/assumption/relation index without changing the
user interface, prompt, tool schema, Router, provider fallback, permission
model, task receipt shape, or SSE payload shape.

Production changes:

- New `codey/research/identity.py` centralizes Research identity helpers:
  URL redaction/digest refs, project refs, path refs, stable refs, digest
  helpers, bounded refs, identifier cleanup, and text clipping.
- New `codey/research/evidence_ledger.py` persists bounded Research object
  records under `research/evidence_ledgers/<session>/<project>.json`. It stores
  schema/kind metadata, source/evidence/claim/assumption/relation maps,
  locator ids/hashes, counts, `ledger_ref`, and `record_id`.
- Evidence ledger writes fail open. Missing or invalid records, bad JSON,
  oversized local ledger files, and write failures return skipped/unavailable
  results instead of breaking the Research run.
- Ledger trimming preserves graph closure. When map caps are hit, Codey keeps
  the newest complete records and prunes older records rather than retaining
  records whose source/evidence/claim/assumption/relation refs no longer exist
  in the corresponding maps. Loaded ledgers are also graph-validated before
  becoming available. Load-time validation uses an allow-list schema: unknown
  raw fields, orphan map entries, and map key / entry id mismatches fail closed
  to unavailable. Known scalar fields must also keep Codey's generated,
  bounded shape, so poisoned `ledger_ref`, `session_ref`, warning, stance,
  status, host, count, or oversized excerpt values are rejected. Source
  `content_hash` is stored only when it is canonical short hex or a sha256 ref;
  malformed typed values, including fake `sha256:` prefixes, are cleared
  instead of persisted or rehashed. Closure checks cover nested claim
  evidence/assumption refs, assumption claim refs, evidence source refs,
  evidence locator source refs, locator/evidence source consistency, and
  relation endpoints.
- If a malformed typed record is pruned for closure, `append_record()` returns
  `skipped=True` with `record_pruned_for_ledger_closure` instead of reporting a
  successful write. Candidate writes are isolated from the loaded ledger until
  the new `record_id` + digest survives trimming and the candidate payload
  passes full canonical validation, so a malformed replacement cannot delete an
  existing good record or poison the next load. If the pruned record is not
  written, the result keeps the previously loaded ledger counts rather than
  reporting temporary counts. Typed records whose `to_jsonable()` fails,
  including malformed nested objects, return `invalid_record` without raising
  from the store.
- `EvidenceLedgerStore.append_record()` now accepts typed `ResearchRecord`
  objects only. Mapping fallbacks are rejected before nested refs can persist
  raw URLs, paths, or source-body-like fields.
- Shared digest refs only preserve real `sha256:<64 hex>` strings. Pseudo
  digest strings are rehashed, so a secret-looking fake digest cannot be stored
  as if it were already safe.
- `TaskRunner`, server state, and the headless JSONL runner now carry an
  optional `EvidenceLedgerStore`. Research completion appends the record and
  Run Trace stores only a bounded evidence-ledger write summary.
- The user-facing Research payload remains unchanged and does not expose
  `research_record` or `evidence_ledger` internals.
- The Capability Registry and Event / Capability Matrix declare
  `research_evidence_ledger` as a quiet durable read model. Architecture tests
  keep Research identity and evidence ledger modules away from provider
  adapters, browser code, tool runtime, TaskRunner/server orchestration, Ghost
  runtime, and plugin loaders.
- Ledger and trace storage exclude raw prompts, raw model responses, raw
  provider errors, raw URLs, raw absolute paths, full source text, and webpage
  bodies. Bounded evidence excerpts remain allowed because they are the
  evidence unit introduced in 0.4.0.

Validation during implementation:

```text
python -m pytest tests\test_research_identity.py tests\test_research_evidence_ledger.py -q -p no:cacheprovider
# 24 passed

python -m pytest tests\test_research_identity.py tests\test_research_evidence_ledger.py tests\test_research_object_model.py tests\test_research.py tests\test_run_trace.py tests\test_task_runner_run_trace.py tests\test_capabilities.py tests\test_event_matrix.py tests\test_architecture.py tests\test_server.py::WebAssetTests::test_runtime_version_matches_release_docs -q -p no:cacheprovider
# 194 passed, 285 subtests passed

python -m ruff check codey tests --no-cache
# All checks passed!

python -m compileall codey tests
# passed

python -m pytest -q -p no:cacheprovider
# 2070 passed, 9 skipped, 576 subtests passed in 428.27s (0:07:08)
```

No live provider A/B is required because 0.4.1, as implemented, is identity,
persistence, projection, and deterministic validation only. It does not change
Research prompts, tool schema prompts, model-visible tool-result wording,
Router behavior, provider fallback ordering, permissions, UI, task receipts, or
SSE payload shape.

## 0.4.0 Evidence Kernel / Research Object Model v1

Codey 0.4.0 adds the first Evidence Kernel piece: every completed Research run
now gets a deterministic, bounded Research object projection. This turns the
existing Research ledger and final report review into local question, source,
evidence, claim, assumption, and relation objects without changing the user
interface, prompt, tool schema, Router, provider fallback, permission model, or
SSE payload shape.

Production changes:

- New `codey/research/object_model.py` builds a `ResearchRecord` from opened
  sources, ledger evidence, citation review metadata, and deterministic report
  section parsing.
- `ResearchRunResult` now carries `research_record` internally. `TaskRunner`
  keeps the existing `research` event payload unchanged and records only a
  bounded Run Trace summary: record id, answer status, object counts,
  unsupported-claim count, and record digest.
- Claim evidence binding is conservative. A citation to one source no longer
  attaches every evidence snippet from that source to every cited claim;
  `supports` relations are only created when the final claim matches the
  evidence claim or bounded excerpt and the evidence stance is valid for that
  report section. Claim `status` is only `evidence_backed`, `unsupported`, or
  `assumption`; support/refutation/limits are expressed by relation kind.
  Conclusion and key evidence sections only accept supporting evidence;
  counter/limitations can use contradicting/refuting evidence as `refutes`
  relations or context as `limits` relations. Empty stance keeps the old
  default-support behavior, but non-empty unknown stance fails closed to
  `unknown` and cannot support conclusion claims.
- Claim graph closure now prunes assumption refs and relations after the
  assumption cap, so records do not contain dangling assumption ids.
- Research URL refs redact userinfo and sensitive query-key variants before
  digesting, including `client_secret`, `refresh_token`, `x-api-key`, `jwt`,
  `session_id`, `authorization`, `bearer`, credential/session variants,
  token/secret/api-key suffixes, protocol-relative URL cases, and malformed or
  no-host URL inputs. Query keys and values are redacted fail-closed before URL
  digesting, and malformed userinfo heads are not digested raw.
- Run Trace accepts only generated-looking `research_record:<16 hex>` ids, so
  fallback or malformed summaries cannot persist raw question-like strings.
- Research runner tool text now reads the v2 `model_text` field directly and
  does not reintroduce the old `output` compatibility fallback.
- The Capability Registry and Event / Capability Matrix declare the
  `research_object_model` projection. Architecture tests keep it away from
  server, TaskRunner orchestration, provider adapters, browser code, tool
  runtime, Ghost runtime, plugin loaders, and file writes.
- `ROADMAP.zh-CN.md` now archives the long 0.3 per-version detail section and
  keeps the detailed active plan focused on 0.4 Evidence Research Runtime.

Validation during implementation:

```text
python -m pytest tests\test_research_object_model.py tests\test_research.py::ResearchBoundaryTests::test_runner_writes_synthesis_and_restore_can_revert_run tests\test_run_trace.py::RunTraceStoreTests::test_research_record_summary_is_bounded_and_digest_only tests\test_task_runner_run_trace.py::test_auto_router_and_research_result_write_structured_trace_refs -q -p no:cacheprovider
# 16 passed

python -m pytest tests\test_research_object_model.py tests\test_research.py tests\test_run_trace.py tests\test_task_runner_run_trace.py tests\test_capabilities.py tests\test_event_matrix.py tests\test_architecture.py tests\test_server.py::WebAssetTests::test_runtime_version_matches_release_docs -q -p no:cacheprovider
# 166 passed, 276 subtests passed

python -m ruff check codey tests --no-cache
# All checks passed!

python -m compileall codey tests
# passed

python -m pytest -q -p no:cacheprovider
# 2042 passed, 9 skipped, 565 subtests passed in 402.76s (0:06:42)

git diff --check
# passed

git diff --cached --check
# passed
```

No live provider A/B is required because 0.4.0, as implemented, is a
deterministic projection only. It does not change Research prompts, tool schema
prompts, model-visible tool-result wording, Router behavior, provider fallback
ordering, permissions, UI, task receipts, or SSE payload shape. A Research A/B
would become necessary only if a later 0.4.0 patch changed prompt text, tool
result wording, report repair behavior, or asked the model to produce the claim
graph directly.

## 0.3.20 Run Details v1

Codey 0.3.20 adds a quiet Run Details projection. Finished task receipts can
show a low-friction `Details` text action that expands in place and explains
the run using bounded ledger/trace metadata. It does not add a drawer, topbar
entry, raw trace viewer, new SSE event type, prompt change, Router change,
provider-fallback change, permission change, or runtime dispatcher.

Production changes:

- New `codey/run_details.py` builds short UI-ready summaries from
  `RunLedgerProjection` and Run Trace manifest metadata: work type, model,
  context, actions, safety, model fallback, and verification. Search tool
  records are classified with read-like actions as inspected items.
- New `GET /api/run_details?session_id=...&run_id=...` returns a read-only
  bounded payload or quiet `available=false` when details are missing.
- Trace manifest reads are byte-first: Run Details uses the local bounded JSON
  reader with `MAX_TRACE_BYTES` and accepts only schema-versioned Run Trace
  manifests with the expected kind.
- New `codey/web/assets/run_details.js` owns the lazy-loaded inline Details
  interaction. Details cache only in memory, expand under the existing
  receipt/status row, scroll into view when opened, and do not persist into
  chat state.
- `index.html` only wires terminal status rows to the asset helper. The Review
  status row intentionally does not add its own Details link, so the same run
  does not show duplicate explanation entries next to the final receipt.
- `app.css` keeps Run Details in the existing design language: no background,
  no rounded card, no shadow, no color accent, subtle `--border-2` divider,
  group-label title, muted labels, and `--text-dim` values.
- The Capability Registry and Event / Capability Matrix now declare the
  `run_details` projection and its `run_details.summary` UI surface. Architecture
  tests keep it read-only and away from provider/browser/task-runner/runtime
  dispatch dependencies.

Validation during implementation:

```text
python -m pytest tests\test_run_details.py tests\test_server.py::ResearchServerHelperTests::test_run_details_response_validation_and_payload tests\test_server.py::ResearchServerHelperTests::test_run_details_response_quiet_unavailable_without_stores tests\test_server.py::WebAssetTests::test_runtime_version_matches_release_docs tests\test_ui.py tests\test_ui_architecture.py tests\test_capabilities.py tests\test_event_matrix.py tests\test_architecture.py -q -p no:cacheprovider
# 117 passed, 261 subtests passed

python -m pytest tests\test_ui_browser_e2e.py -q -p no:cacheprovider
# 1 passed

python tools\ui_e2e.py --artifacts .run-details-visual --json
# passed; inspected ui-run-details.png for the inline receipt layout, then removed the temporary artifacts

python -m pytest -q -p no:cacheprovider
# 2026 passed, 9 skipped, 549 subtests passed

python -m ruff check . --no-cache
# All checks passed!

python -m compileall codey tests
# passed

git diff --check
# passed
```

No live provider A/B is required because 0.3.20 does not change prompt text,
tool schema prompt, model-visible tool-result wording, Router behavior,
provider fallback ordering, permissions, Research/Writer/Review semantics,
task receipts, or SSE payload shape. The release touches UI, so browser e2e
and screenshot inspection were used to verify that Details stays quiet,
inline, and visually aligned with `DESIGN.md`.

## 0.3.19 Built-in Profiles v1

Codey 0.3.19 adds a read-only built-in profile catalog. It records default
strategy tendencies as metadata and architecture tests, without adding a
profile UI, configuration platform, plugin system, router, permission engine,
prompt patch layer, provider selector, or runtime dispatcher.

Production changes:

- New `codey/builtin_profiles.py` defines fixed built-in profile metadata for
  `default`, `research_heavy`, `review_strict`, `local_only`, and `beginner`.
- New `docs/codey_builtin_profiles.md` documents the profile boundaries and v1
  non-goals: no UI, no prompt patches, no provider fallback changes, no Router
  changes, no permission changes, and no SSE/receipt shape changes.
- New `tests/test_builtin_profiles.py` locks stable ids, JSON export,
  fingerprinting, capability references, explicit permission profile names,
  provider scopes, non-override flags, no permission relaxation, empty prompt
  patches, local-only Research-network behavior, review-strict write defaults,
  local-only absence of a Research permission default, and beginner-facing copy
  that avoids principle/design internal implementation terms.
- `server.State` owns the built-in profile registry, and `TaskRunner` carries it
  as metadata only. `TaskRunner` does not branch on profile metadata.
- The Capability Registry now declares a `builtin_profiles` capability owned by
  `codey.builtin_profiles`; the registry remains read-only metadata.
- Architecture tests keep `builtin_profiles.py` from importing provider,
  browser, server, task-runner, tool-runtime, Research runner, dynamic import,
  or plugin-host code.

Validation during implementation:

```text
python -m pytest tests\test_builtin_profiles.py tests\test_capabilities.py tests\test_architecture.py tests\test_server.py tests\test_task_runner_router.py tests\test_permission_profiles.py tests\test_provider_capabilities.py tests\test_event_matrix.py -q -p no:cacheprovider
# 234 passed, 1 skipped, 279 subtests passed

python -m pytest -q -p no:cacheprovider
# 2015 passed, 9 skipped, 535 subtests passed

python -m ruff check . --no-cache
# All checks passed!

python -m compileall codey tests
# passed

git diff --check
# passed
```

No live provider A/B is required because 0.3.19 does not change prompt text,
tool schema prompt, model-visible tool-result wording, Router behavior,
provider fallback ordering, permissions, Research/Writer/Review semantics, UI
structure, SSE payload shape, or task receipts.

## 0.3.18 Event / Capability Matrix v1

Codey 0.3.18 adds a tested Event / Capability Matrix and moves the existing
Web/SSE `RunEvent` UI projection into `codey.events`. It keeps the release as
architecture metadata plus a small event-projection refactor, not an event bus
or runtime dispatcher.

Production changes:

- New `docs/codey_event_matrix.md` records event ids, producers, consumers,
  linked capabilities, durable state, model visibility, UI visibility, policy
  requirements, trace requirements, and privacy boundaries.
- New `tests/test_event_matrix.py` parses that markdown matrix and rejects
  duplicate event ids, unknown capability or durable-state names, missing
  Prompt Envelope / Run Trace coverage for model-visible rows, missing policy
  declarations, missing trace consumers, unknown UI surfaces, and raw-payload
  privacy regressions.
- The matrix now declares `review.recent_log` as the model-visible projection
  rendered from `RunEvent` history for Review prompts. UI/SSE `run_event.*`
  rows remain scoped to UI and ledger projections.
- `codey.events.display_tool()` now owns the existing Research tool display
  mapping, and `codey.events.run_event_ui_payload()` owns the existing Web/SSE
  payload projection for `RunEvent`.
- `TaskRunner` now calls the shared UI/SSE projection and no longer defines
  local `_ui_event` or `_display_tool` helpers. `run_event_payload()` and
  RunLedger projection remain separate because they serve different consumers.

Validation during implementation:

```text
python -m pytest tests\test_event_matrix.py tests\test_events.py tests\test_server.py tests\test_ui.py tests\test_run_ledger.py tests\test_run_trace.py tests\test_capabilities.py tests\test_architecture.py -q -p no:cacheprovider
# 279 passed, 1 skipped, 246 subtests passed

python -m pytest -q -p no:cacheprovider
# 1999 passed, 9 skipped, 518 subtests passed

python -m ruff check . --no-cache
# All checks passed!

python -m compileall codey tests
# passed

git diff --check
# passed
```

No live provider A/B is required because 0.3.18 does not change prompt text,
tool schema prompt, model-visible tool-result wording, Router behavior,
provider fallback ordering, permissions, Research/Writer/Review semantics, UI
structure, SSE payload shape, or task receipts.

## 0.3.17 Action Policy Pipeline v1

Codey 0.3.17 adds a monotonic local action policy pipeline. Local runtime
actions are evaluated as bounded `ActionSubject` metadata before execution and
resolve to `allow`, `ask_user`, or `deny`, with `deny` unable to be weakened by
later guards.

Production changes:

- New `codey/action_policy.py` centralizes deterministic guards for permission
  profiles, workspace paths, write scope, run-command allowlists, shell
  approval availability, Research URLs, Local context actions, provider
  fallback metadata, and managed-output retention limits.
- `tool_runtime.run_command_raw()` now evaluates the shared run-command policy
  before execution with an explicit permission profile, while preserving the
  existing allowlist semantics.
- `agent.py` evaluates tool calls before local execution. Policy denial returns
  a structured `ToolOutcome` with `error_code="policy_denied"`; shell requests
  still use the existing approval flow when approval is available.
- `research/url_policy.py` keeps its public API and denial text while delegating
  URL safety checks to the shared policy source; invalid URL ports return a
  policy denial reason instead of raising parser exceptions.
- Managed output artifact writes now pass through policy metadata; size/count
  denials and missing/insufficient profiles skip full-artifact retention without
  changing the bounded model result text.
- Unknown action kinds are denied by the policy pipeline instead of defaulting
  to allow.
- `codey.action_policy.__all__` exposes only the narrow core API; low-level
  run-command helpers stay outside the star-import public surface.
- Run Trace manifests now include bounded `policy_decisions` entries with
  kind, decision, guard id, reason code, phase, subject ref, and display digest
  metadata only. They do not store raw commands, raw URLs, stdout, source text,
  or webpage bodies, and mapping fallbacks must provide digest-shaped refs.
- `policy_guard` capability metadata now declares `action_policy_boundary`;
  the Capability Registry remains read-only and does not dispatch runtime work.

Validation during implementation:

```text
python -m pytest tests\test_action_policy.py tests\test_tool_runtime.py tests\test_research.py::ResearchBoundaryTests::test_url_policy_rejects_private_targets_without_network tests\test_run_trace.py tests\test_managed_outputs.py tests\test_agent_tools.py tests\test_agent.py tests\test_task_runner_run_trace.py tests\test_capabilities.py tests\test_architecture.py tests\test_server.py -q -p no:cacheprovider
# 428 passed, 6 skipped, 80 subtests passed

python -m pytest -q -p no:cacheprovider
# 1989 passed, 9 skipped, 342 subtests passed

python -m ruff check . --no-cache
# All checks passed!

python -m py_compile <changed Python files>
# passed

git diff --check
# passed
```

No live provider A/B is required because 0.3.17 does not change prompt text,
tool schema prompt, model-visible normal tool-result wording, Router behavior,
provider fallback ordering, permissions, Research/Writer/Review semantics, UI
structure, SSE event shape, or task receipts.

## 0.3.16 Tool Contract v2

Codey 0.3.16 cleanly migrates local tool results from a shared `output`
string to explicit projections: `model_text`, `presentation`, `audit`, and
`canonical`. It does not keep an `output` compatibility layer.

Production changes:

- `ToolOutcome` and `ToolResult` now expose `model_text` as the only
  model-visible result text field. Legacy `output` and top-level
  managed-output fields are removed.
- `presentation` feeds UI/SSE/receipt result/status helpers, while `audit`
  carries ledger/local-audit metadata such as `managed_output`.
- `presentation`, `audit`, and `canonical` are sanitized into bounded
  JSON-safe mappings at the result-type boundary. Non-JSON values, non-finite
  floats, overlong strings, excessive depth, long keys, and excessive items are
  bounded and reported through projection warnings.
- Non-mapping projections are accepted and converted to warning-bearing empty
  mappings before defaults are added. `_projection_warnings` is treated as a
  reserved sanitizer field, so real sanitizer warnings cannot be shadowed by
  caller input.
- Managed-output audit metadata is normalized before model footer, UI/SSE, and
  RunLedger consumption: only `out_[A-Za-z0-9_.-]{1,80}` handles are accepted,
  invalid handles are ignored, invalid byte counts become `0`, and invalid
  `sha256` values are emptied unless they are 64-character lowercase hex.
- `model_text` is finalized by an idempotent helper so truncated-result and
  managed-output footer text remain model-visible without making codecs parse
  audit fields.
- Coding and Research tool-result codecs render only `model_text`; tests pin
  that `presentation`, ordinary `audit`, and `canonical` sentinels do not
  enter prompts.
- Research runner outcomes now expose the same presentation/status/managed
  output helper surface used by Coding run events.
- Manual A/B helpers that construct or inspect `ToolOutcome` were migrated to
  `model_text` so the clean-cut contract is consistent outside automated
  tests too.

Validation during implementation:

```text
python -m pytest tests\test_protocols.py tests\test_tool_runtime.py tests\test_managed_outputs.py tests\test_run_ledger.py tests\test_server.py::TaskRunnerUiEventTests -q -p no:cacheprovider
# 148 passed, 3 skipped, 49 subtests passed

python -m pytest -q -p no:cacheprovider
# 1966 passed, 9 skipped, 335 subtests passed

python -m ruff check . --no-cache
# All checks passed!

python -m py_compile <changed Python files>
# passed

git diff --check
# passed
```

No live provider A/B is required because 0.3.16 does not change the tool schema
prompt, Router behavior, provider fallback, permissions, Research/Writer/Review
semantics, UI structure, SSE event shape, or task receipts. The model-visible
tool-result wording is preserved through `model_text` parity tests.

## 0.3.15 Internal Capability Registry v1

Codey 0.3.15 adds a read-only internal capability registry. It records built-in
capability boundaries as metadata, without becoming a plugin system, runtime
dispatcher, provider/router decision point, permission engine, UI entry point,
or Run Trace schema change.

Production changes:

- New `codey/capabilities.py` defines `CapabilitySpec`, `CapabilityRegistry`,
  `builtin_capability_registry()`, stable JSON export, fingerprinting, and
  validation.
- The built-in registry covers provider factory, provider capability hints,
  agent runner, tool runtime, Research runner, Review runner, Local context,
  changes presenter, RunLedger, Run Trace, Prompt Envelope, and policy guard.
- Registry validation rejects unknown capability dependencies, permission
  profiles, UI surfaces, durable states, third-party flags, user-choice override
  flags, missing Prompt Envelope / Run Trace edges for model-visible
  capabilities, and missing policy guard edges for policy-bound capabilities.
- `server.State` owns the built-in registry, and `TaskRunner` carries it as
  metadata only. It is not used for provider selection, Router decisions,
  permission profile selection, prompt assembly, tool dispatch, UI, SSE,
  receipts, or fallback behavior.
- Architecture tests keep `capabilities.py` metadata-only: it cannot import
  provider/browser/server/task-runner/tool/research runtime modules or use
  plugin-loader shaped APIs.

Validation during implementation:

```text
python -m pytest tests\test_capabilities.py tests\test_architecture.py tests\test_server.py tests\test_task_runner_run_trace.py tests\test_prompt_envelope.py -q -p no:cacheprovider
# 201 passed, 1 skipped, 63 subtests passed

python -m pytest -q -p no:cacheprovider
# 1947 passed, 9 skipped, 319 subtests passed

python -m ruff check . --no-cache
# All checks passed!

python -m py_compile codey\capabilities.py codey\server.py codey\task_runner.py tests\test_capabilities.py tests\test_architecture.py tests\test_server.py
# passed

git diff --check
# passed
```

No live provider A/B is required because 0.3.15 does not change prompt text,
Router behavior, provider fallback, permissions, Research/Writer/Review
semantics, UI, SSE events, task receipts, or runtime dispatch.

## 0.3.14 Prompt Envelope v1

Codey 0.3.14 adds a lightweight prompt envelope for model-visible sections and
a shared fail-open trace sink. It keeps the rendered prompt text unchanged while
making prompt composition auditable through Run Trace metadata.

Production changes:

- New `codey/prompt_envelope.py` defines `PromptEnvelopeSection`,
  `PromptEnvelope`, rendered section metadata, and `FailOpenPromptTrace`.
- `agent.py`, `task_runner.py`, and `research/runner.py` now use the shared
  trace sink instead of local `trace_call` / `_trace` helpers.
- Research intro prompt assembly now goes through `PromptEnvelope` while
  preserving the existing `\n\n` join shape.
- Coding keeps the existing prompt text shape, including the single newline
  before `User task`.
- Run Trace prompt sections now include bounded `purpose`, `model_visible`, and
  source-ref fallback metadata, while still writing only digests and bounded
  metadata.
- Provider-send prompt sections still flush at the model boundary before
  provider calls. TaskRunner secondary snippets use non-boundary
  `secondary_input_prepared` metadata; non-boundary metadata remains checkpoint
  batched.
- Trace-disabled local-context and secondary-input helpers now return early
  instead of scanning sections.
- Chat consensus does not record a `chat_outbound_prompt` when the selected
  chat provider is not actually sent that prompt.
- Project-audit advisor source refs include advisor id, and Run Trace
  prompt-section dedup includes `purpose`.
- `PromptEnvelope` does not import provider control code; control-teaching
  cancellation still propagates by exception name.
- `PromptEnvelope` keeps the v1 API surface minimal: sections are passed at
  construction time and rendered without an unused mutable builder method.

Validation during implementation:

```text
python -m pytest tests\test_task_runner_run_trace.py tests\test_server.py tests\test_consensus.py tests\test_run_trace.py tests\test_prompt_envelope.py -q -p no:cacheprovider
# 219 passed, 2 skipped

python -m pytest -q -p no:cacheprovider
# 1929 passed, 9 skipped, 276 subtests passed

python -m ruff check codey tests --no-cache
# All checks passed!

python -m py_compile codey\prompt_envelope.py codey\task_runner.py codey\consensus.py codey\run_trace.py codey\server.py tests\test_prompt_envelope.py tests\test_run_trace.py tests\test_task_runner_run_trace.py tests\test_consensus.py tests\test_server.py tests\test_architecture.py
# passed

git diff --check
# passed
```

No live provider A/B is required for 0.3.14 because it does not change prompt
text, Router behavior, Research/Writer/Review semantics, provider fallback,
permissions, UI, SSE events, or task receipts. The release is gated by prompt
parity and deterministic regression tests.

## 0.3.13 Run Trace Manifest v1

Codey 0.3.13 adds a local run trace manifest sidecar for each task. It records
bounded audit metadata about mode/provider selection, prompt section digests,
tool contract hashes, Local context refs, Research refs, and provider fallback
facts. It does not change prompts, Router behavior, Research/Writer behavior,
provider fallback policy, permissions, UI, SSE events, or task receipts.

Production changes:

- New `codey/run_trace.py` writes `run_traces/<session>/<run>.json` sidecars
  keyed by `run_id`.
- `TaskRunner` opens a run-scoped recorder and records structured router
  outcome, provider switches, terminal status, provider failure categories, and
  Research note/source refs.
- Hybrid runs record both Research and Writer phase profile/contract entries.
- Secondary model calls for consensus, project audit, and review record
  digest-only prompt input sections without storing raw diff or summaries.
- Review trace records the same precomputed review impact map that is passed to
  the reviewer prompt, avoiding a second bounded project scan.
- Research source refs store hostname only for visible host metadata; URL
  userinfo and ports are not written into the trace.
- High-frequency trace metadata uses checkpoint batching; run start, router,
  fallback, provider failure, warning, and finish still flush immediately.
- Model-visible prompt section records at provider or secondary-model send
  boundaries flush immediately before the model call.
- `agent.py` and `research/runner.py` record prompt section digests and character
  counts before provider sends, without writing raw prompt text.
- `context_source.py` now has a metadata helper that preserves exact rendered
  context text while exposing section metadata for trace manifests.
- Coding and Research tool contracts now expose stable model-visible contract
  hashes.
- `State.forget_conversation()` deletes the forgotten session's run trace
  sidecars.

Validation during implementation:

```text
python -m pytest tests\test_run_trace.py tests\test_task_runner_run_trace.py tests\test_context_source.py tests\test_protocols.py tests\test_research_protocol_contract.py -q -p no:cacheprovider
# 89 passed, 33 subtests passed

python -m pytest tests\test_run_trace.py tests\test_task_runner_run_trace.py tests\test_context_source.py tests\test_protocols.py tests\test_research_protocol_contract.py tests\test_agent.py tests\test_research.py tests\test_server.py -q -p no:cacheprovider
# 437 passed, 3 skipped, 33 subtests passed

python -m pytest -q -p no:cacheprovider
# 1908 passed, 9 skipped, 276 subtests passed

python -m ruff check .
# All checks passed!

python -m py_compile codey\__init__.py codey\agent.py codey\context_source.py codey\headless_runner.py codey\protocols\base.py codey\protocols\json_codec.py codey\research\protocols.py codey\research\runner.py codey\research\tool_contract.py codey\run_trace.py codey\server.py codey\task_runner.py codey\tool_definition.py
# passed

git diff --check
# passed
```

No live provider A/B is required for 0.3.13 because model-visible behavior is
unchanged. If a live web-provider smoke hits provider-side rate limiting, treat
that as an environmental check result rather than a 0.3.13 behavior regression.

## 0.3.12 Research Notes v2

Codey 0.3.12 upgrades the Research drawer Notes tab from a note-id/excerpt log
into a readable, local-only audit view. It does not change the Research prompt,
runner, provider behavior, Router, Writer path, or permission model.

Production changes:

- `codey/web/assets/research_drawer.js` now renders Notes as grouped note
  cards: `Selected note`, `Synthesis`, `Created notes`, and `Updated notes`.
  Empty sections are skipped; fully empty runs show one quiet `No notes
  recorded` state.
- Notes use bounded Markdown previews through `CodeyRender.renderMarkdown`.
  Long bodies are clipped at a fixed preview budget with local `Show more` /
  `Show less` controls that do not write state.
- `codey/web/assets/render.js` now supports Markdown blockquotes in the same
  escaped, minimal renderer used for assistant text and Research graph details.
- Per-note source chips are derived only from saved local provenance:
  `note.sources`, `citationMap`, `openedSources`, and `sourceUrls`. Clickable
  chips are restricted to `http:` / `https:` URLs and open with
  `noopener,noreferrer`.
- The Notes-tab source URL section was removed. Sources remain available in the
  `Sources` tab and as per-note chips.

Validation during implementation:

```text
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --check codey\web\assets\research_drawer.js
# passed

C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --check codey\web\assets\render.js
# passed

python -m pytest tests\test_ui.py tests\test_server.py -q -p no:cacheprovider
# 208 passed, 1 skipped

python -m pytest tests\test_ui.py tests\test_server.py tests\test_ui_architecture.py -q -p no:cacheprovider
# 219 passed, 1 skipped

python -m pytest -q -p no:cacheprovider
# 1886 passed, 9 skipped, 276 subtests passed
```

No live provider A/B is required for 0.3.12 because the model-visible Research
behavior is unchanged. The appropriate coverage is deterministic UI/server
tests. A local browser smoke for Notes, source chips, and Show more/less is
recommended before release when interactive browser verification is available.

## 0.3.11 Local Context Control Surface v1

Codey 0.3.11 adds a quiet Local context audit drawer for local state. It is an
inspection and control surface, not a new workspace, personality panel, routing
authority, or permission editor.

Production changes:

- New `codey/ghost/control_surface.py` provides a bounded UI presenter and
  action dispatcher.
- Server routes `GET /api/ghost/summary`, `POST /api/ghost/action`, and
  `GET /api/ghost/export` expose local audit controls. The summary response
  does not include full chat transcripts, Research bodies, webpage/source
  snippets, source code, prompts, raw provider replies, or raw provider errors.
- The topbar `...` menu now contains `Local context`; it opens a right drawer
  that reuses the Changes/Research drawer language and is mutually exclusive
  with Changes and Research.
- The drawer shows a single grouped view: `Recent focus`, `Pending review`,
  `Active preferences`, `Follow-ups`, and `Health`. User-facing UI avoids internal
  Ghost system terms.
- Empty Local context renders one quiet empty state, Settings is visually
  separated from audit content, and Research Notes use plain note text styling
  instead of diff/code block styling.
- The composer context row now shows only `Choose folder · Research`; the
  active provider/model is shown only in the bottom provider picker.
- v1 actions are accept/reject candidate, queue/reject work item,
  enable/disable updates, delete current chat/project data, reset all, and
  export. It does not add demote, prompt/provider/router/tool-permission
  controls, or direct free-form memory editing.
- The drawer binds to the loaded session/project scope and closes on chat or
  project switches. Backend actions validate the candidate/work item scope
  before mutating state, so stale drawer actions fail closed.
- Local context loading binds the requested scope before fetching, and stale
  loading/error callbacks are ignored after chat/project switches.
- Obsolete `ctx-provider` composer-context compatibility was removed after the
  provider/model selector moved fully to the bottom provider picker.
- Affinity Hebbian evidence refs are materialized before bounding, preventing
  generator-object reprs from entering local association evidence and breaking
  replay idempotency.

Validation during implementation:

```text
python -m py_compile codey\ghost\affinity.py codey\ghost\control_surface.py codey\server.py codey\__init__.py
# passed

C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --check codey\web\assets\local_context_drawer.js
# passed

C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --check codey\web\assets\research_drawer.js
# passed

python -m pytest tests\test_ui.py -q -p no:cacheprovider
# 57 passed

python -m pytest tests\test_ghost_control_surface.py tests\test_ui.py -q -p no:cacheprovider
# 68 passed

python -m pytest tests\test_ui.py tests\test_ui_architecture.py tests\test_ghost_control_surface.py tests\test_server.py -q -p no:cacheprovider
# 230 passed, 1 skipped

python -m pytest tests\test_ghost_affinity.py -q -p no:cacheprovider
# 19 passed

python -m pytest tests\test_ui.py tests\test_ui_architecture.py tests\test_ghost_control_surface.py tests\test_server.py tests\test_ghost_affinity.py -q -p no:cacheprovider
# 249 passed, 1 skipped

python -m pytest tests\test_ghost_control_surface.py tests\test_server.py tests\test_ui.py tests\test_ui_architecture.py tests\test_architecture.py -q -p no:cacheprovider
# 235 passed, 1 skipped, 20 subtests passed

python -m pytest -q -p no:cacheprovider
# 1886 passed, 9 skipped, 276 subtests passed

python -m ruff check .
# All checks passed!
```

No live provider A/B is required for 0.3.11. This release does not change the
model-visible prompt, Router, Research/Writer execution paths, provider
fallback, or permission boundaries. The appropriate coverage is deterministic
API/UI/architecture tests plus local browser smoke for opening the drawer,
refreshing it, drawer mutual exclusion, stale-scope closure, action review
updates, and bounded summary output.

## 0.3.10 Affinity Index v1

Codey 0.3.10 adds a bounded local Affinity Index. It is a deterministic
association ledger, not a truth layer, permission layer, router authority, or
autonomous runner. It records which bounded local facts tend to co-occur and
uses that only for low-risk ordering.

Production changes:

- New `codey/ghost/affinity.py` stores `affinity_events.jsonl` as the source of
  truth and `affinity.json` as a rebuildable projection. Mutating sync is
  blocked when the event log is unreadable or over the byte cap.
- Affinity sync reads only bounded local facts from Hebbian, Work Queue,
  Research Interest candidates, Router audit metadata, provider failure kinds,
  and task outcome summaries.
- It does not store full chat text, Research bodies, webpage/source snippets,
  source code, prompts, provider raw replies, or raw provider error messages.
- Default consumption is limited to ordering: Ghost Directive can reorder
  already-renderable typed memory nodes, Work Queue strict continuation claim
  order can get a small boost, and Research Interest candidates can get a
  priority boost. It does not generate prompt text or bypass execution
  boundaries.
- `ghost export`, `ghost reset --yes`, `ghost delete-scope`,
  `forget_conversation()`, and Cognitive Sleep maintenance cover Affinity.
  `ghost disable` prevents automatic sync and hint consumption.

Validation during implementation:

```text
python -m py_compile codey\ghost\affinity.py codey\ghost\directive.py codey\ghost\work_queue.py codey\ghost\sleep.py codey\server.py codey\task_runner.py codey\cli.py codey\knowledge\research_interest.py tests\manual\ghost_affinity_ab.py tests\manual\ghost_affinity_quality_ab.py
# passed

python -m pytest tests\test_ghost_affinity.py -q
# 19 passed, 1 pytest cache warning

python -m pytest tests\test_ghost_affinity.py tests\test_ghost_work_queue.py tests\test_research_interest_queue.py tests\test_ghost_directive.py -q
# 89 passed, 1 pytest cache warning, 34 subtests passed

python -m pytest tests\test_ghost_sleep.py -q
# 12 passed, 1 pytest cache warning, 6 subtests passed

python -m pytest tests\test_task_runner_affinity.py -q
# 4 passed, 1 pytest cache warning

python -m pytest tests\test_cli.py tests\test_architecture.py -q
# 22 passed, 1 pytest cache warning, 19 subtests passed

python -m pytest tests\test_server.py -k "forgetting_session_clears_only_its_terminal_event" -q
# 1 passed, 148 deselected, 1 pytest cache warning

python -m pytest tests\test_task_runner_work_queue.py tests\test_task_runner_affinity.py -q
# 12 passed, 1 pytest cache warning

python -m pytest tests\test_ghost_affinity.py tests\test_task_runner_affinity.py tests\test_ghost_work_queue.py tests\test_task_runner_work_queue.py tests\test_research_interest_queue.py tests\test_ghost_directive.py tests\test_ghost_sleep.py tests\test_cli.py tests\test_server.py tests\test_architecture.py -q
# 284 passed, 1 skipped, 1 pytest cache warning, 59 subtests passed

python -m pytest tests\test_ghost_directive.py tests\test_research_interest_queue.py -q
# 42 passed, 1 pytest cache warning, 34 subtests passed

python -m pytest -q
# 1863 passed, 9 skipped, 1 pytest cache warning, 273 subtests passed

python -m ruff check codey tests
# All checks passed!

python -B tests\manual\ghost_affinity_ab.py --self-test --provider deepseek --output tests\manual\results\ghost_affinity_selftest.json
# baseline 5/5; affinity 5/5

python -B tests\manual\ghost_affinity_quality_ab.py --self-test --provider deepseek --output tests\manual\results\ghost_affinity_quality_selftest.json
# baseline 0/2; affinity 2/2; uplift +2
```

2026-08-13 Affinity Index production-spine A/B, five-case matrix, run one
provider per fresh webpage tab. This validates the real `TaskRunner` spine,
provider prompt submission, directive prompt ordering, queue claim ordering,
and boundary checks. Research/Writer/Review bodies are safe stubs, so this is
not a full live Research or project-writing model-quality A/B:

```text
DeepSeek: baseline 5/5; affinity 5/5
Qwen:     baseline 5/5; affinity 5/5
MiMo:     baseline 5/5; affinity 5/5
GLM:      baseline 5/5; affinity 5/5
StepFun:  baseline 5/5; affinity 5/5
```

2026-08-13 Affinity Index quality/uplift A/B, two-case matrix, run one provider
per fresh webpage tab. This uses the production `TaskRunner` chat path and real
provider replies, then scores both arms with the same metric:
`first_line_uses_affinity_target_alpha_marker`. A result is `ok` only when all
rows execute cleanly and affinity has strictly more target hits than baseline.
Rows also check that provider replies and the model-visible prompt avoid
internal Ghost/Affinity terms while allowing the neutral `Local Context` label.
This validates Directive ordering uplift on a controlled preference choice; it
is not a broad Research, Writer, or planning quality benchmark:

```text
DeepSeek: baseline 2/2; affinity 2/2; uplift 0
Qwen:     baseline 0/2; affinity 2/2; uplift +2
MiMo:     baseline 0/2; affinity 2/2; uplift +2
GLM:      baseline 0/2; affinity 2/2; uplift +2
StepFun:  baseline 0/2; affinity 2/2; uplift +2
```

DeepSeek executed cleanly with no prompt/reply leakage, but this marker probe
did not show uplift because the baseline arm also matched the target marker.

## 0.3.9 Research Interest Queue v1

Codey 0.3.9 improves the existing Ghost Work Queue research sources. It does
not create a second Research queue and does not run background web searches.
Research follow-ups now come from typed `KnowledgeNote.open_questions` metadata
or structured Concept Graph missing links, then map into the existing
`GhostWorkItem` claim/running/done/blocked state machine.

Production changes:

- New `codey/knowledge/research_interest.py` builds bounded
  `ResearchInterestCandidate` rows without importing Ghost state-machine types.
- Research synthesis / decision notes now support structured
  `open_questions` frontmatter, cached in the rebuildable SQLite index.
  Research Interest harvesting and Research Briefs read this typed field only;
  they do not parse Markdown `Open questions` sections.
- `ConceptGraphBuilder.missing_links_for_session(..., strict_scope=True)` returns
  structured `MissingConceptLink` rows from active support refs in the current
  session/project. The queue never reverse-parses UI excerpt text.
- Strong supported concept gaps and structured Research note questions can
  become queued Research items. Weak concept gaps remain candidate
  `open_question` items.
- Completion still requires `research:*` proof. Concept refs explain why a
  question is worth checking; they do not prove it was answered.
- TaskRunner harvests these candidates during post-turn Work Queue sync only.
  Router, Directive, Continuity prompt text, Research prompt context, UI,
  permissions, and provider adapter behavior stay unchanged.

Validation during implementation:

```text
python -m pytest tests\test_research_interest_queue.py tests\test_ghost_continuity.py tests\test_ghost_work_queue.py tests\test_task_runner_work_queue.py tests\test_ghost_sleep.py -q
# 75 passed, 1 pytest cache warning, 6 subtests passed

python -m pytest tests\test_research_interest_queue.py tests\test_knowledge.py tests\test_research.py -k "research_interest or open_questions or synthesis or protocol or done or brief or note_relations" -q
# 26 passed, 104 deselected, 1 pytest cache warning

python -m pytest tests\test_research_interest_queue.py tests\test_ghost_continuity.py tests\test_ghost_work_queue.py tests\test_task_runner_work_queue.py tests\test_ghost_sleep.py tests\test_cli.py tests\test_server.py tests\test_architecture.py -q
# 244 passed, 1 skipped, 1 pytest cache warning, 24 subtests passed

python -m pytest -q
# 1835 passed, 9 skipped, 1 pytest cache warning, 270 subtests passed

python -m ruff check codey tests
# All checks passed!

python -B tests\manual\ghost_research_interest_queue_production_ab.py --self-test
# self-test ok
```

2026-08-12 Research Interest Queue production-spine A/B, six-case matrix, run
one provider per restarted Edge CDP session:

```text
DeepSeek: baseline 3/6; queue 6/6
Qwen:     baseline 3/6; queue 6/6
MiMo:     baseline 3/6; queue 6/6
StepFun:  baseline 3/6; queue 6/6
GLM:      partial, baseline 2/4; queue 4/4. The remaining cases timed out
          because the webpage was slow/self-searching, not because of a local
          queue failure.
```

The intended baseline misses are the saved follow-up cases: without Research
Interest Queue consumption, plain "continue" remains Chat. With the queue arm,
Codey claims the saved item, dispatches Research, and marks it done only with
Research proof. The matrix also verifies no-queue fallback, weak concept
candidate non-consumption, contentful continuation not consuming the queue,
missing proof blocking, and no internal Ghost / Work Queue / Concept Graph
leakage.

## 0.3.8 Ghost Work Queue v1

Codey 0.3.8 adds a bounded local work-item queue. Existing local facts can
create auditable follow-ups, but nothing runs in the background. Only
`intent=auto` plus a strict continuation request such as `continue`, `next
item`, `继续`, or `下一个` can claim one queued item before Router runs.

Production changes:

- New `codey/ghost/work_queue.py` stores `work_events.jsonl` as the audit source
  of truth and `work_items.json` as a rebuildable projection.
- Work items sync from bounded continuity open questions, structured Research
  note `open_questions`, interrupted checkpoints, run ledger failure
  projections, and review follow-ups. They do not store full user text, prompts,
  source files, Research bodies, or webpage text.
- Claimed items map only to existing modes: Research, Project Writer, or
  review-only. Work Queue cannot grant permissions, approve shell commands,
  choose tool arguments, or execute by itself.
- Completion requires local proof refs from the task event, run ledger, receipt,
  diff, Research report, or review result. Missing proof blocks the item.
- Existing Ghost export/reset/delete-scope controls now cover work queue files.
  Thin CLI controls were added for listing, queueing, and rejecting work items.
- Cognitive Sleep includes work queue health and threshold-based compaction,
  but still does not execute tasks, call providers, emit UI events, or change
  prompts.

Validation during implementation:

```text
python -m pytest tests\test_ghost_work_queue.py tests\test_task_runner_work_queue.py tests\test_ghost_work_queue_ab.py -q
# 36 passed, 1 pytest cache warning

python -m pytest tests\test_ghost_work_queue.py tests\test_task_runner_work_queue.py tests\test_ghost_work_queue_ab.py tests\test_ghost_sleep.py tests\test_cli.py tests\test_server.py tests\test_architecture.py -q
# 216 passed, 1 skipped, 1 pytest cache warning, 24 subtests passed

python -m pytest -q
# 1777 passed, 9 skipped, 1 pytest cache warning, 270 subtests passed

python -m ruff check codey tests
# All checks passed!

python -B tests\manual\ghost_work_queue_production_ab.py --self-test
# self-test ok
```

Live A/B is required before release because strict continuation can change the
real execution path. Run one provider per restarted Edge CDP session and write
each output file atomically:

```text
python -B tests\manual\ghost_work_queue_production_ab.py --provider deepseek --port 9222 --output tests\manual\results\ghost_work_queue_production_deepseek.json
```

2026-08-11 Ghost Work Queue production-spine A/B, five-case matrix, run one
provider per restarted Edge CDP session:

```text
DeepSeek: baseline 4/5; queue 5/5
Qwen:     baseline 4/5; queue 5/5
MiMo:     baseline 4/5; queue 5/5
GLM:      baseline 4/5; queue 5/5
StepFun:  baseline 4/5; queue 5/5
```

The baseline miss was the intended research follow-up case: with a queued
Research item, plain "continue" stayed in Chat. The queue arm claimed the item
and dispatched Research. The other cases verified no-queue fallback, project
follow-up dispatch, review-only dispatch, and contentful continuation not
consuming the queue.

## 0.3.7 Ghost Router v1

Codey 0.3.7 adds production automatic routing for `intent=auto`. Before
`task_start`, Codey asks a fresh provider tab for a bounded JSON route decision
and then the production `TaskRunner` consumes the final mode. The router can
choose only `chat`, `planning_readonly`, `research`, `project`, `hybrid`, or
`review`; it cannot grant permissions, approve shell commands, choose tool
arguments, or override manual user mode.

Production changes:

- New `codey/ghost/router.py` stores bounded route audit in
  `router_events.jsonl` and `router_state.json`.
- Audit does not store full user tasks, raw prompts, raw provider replies, or
  model reason text. Diagnostics are local error codes rather than exception
  messages.
- Parser policy is strict for production routing: fenced JSON is accepted, but
  prose-wrapped, array-wrapped, or multiple JSON objects are rejected.
- Local hard rules block project-reading/writing modes when the user explicitly
  asks for chat without project file access.
- `TaskCancelled` / `ControlTeachCancelled` stops the task instead of falling
  back to the baseline route.
- Event audit failure prevents a route from changing behavior. Projection or
  compaction failure after a successful event append only adds warnings. Router
  event rewrites use `router_events.jsonl` as the source of truth, and missing
  events are bootstrapped from projection before new audit is appended.
- Production router preflight uses one short attempt before `task_start`; manual
  A/B scripts can still use wider timeouts.
- Added review-only mode. It reviews current Git or snapshot diffs without
  starting Writer, connecting the main provider, repairing, or editing files.
- `python -m codey agent --json --auto` opts headless runs into auto routing.
  Existing Ghost export/reset/delete-scope controls cover router audit files.

Validation:

```text
python -m pytest -q
# 1740 passed, 9 skipped, 1 pytest cache warning, 267 subtests passed

python -m pytest tests\test_ghost_router.py tests\test_task_runner_router.py tests\test_ghost_router_ab.py tests\test_cli.py tests\test_headless_runner.py tests\test_server.py tests\test_ui.py tests\test_architecture.py -q
# 276 passed, 1 skipped, 1 pytest cache warning, 17 subtests passed

python -m ruff check .
# All checks passed!

python -B tests\manual\ghost_router_ab.py --self-test
# self-test ok

python -B tests\manual\ghost_router_production_ab.py --self-test
# self-test ok

git diff --check
# passed
```

Live A/B:

```text
DeepSeek router-only:        10/10; production-spine: 10/10
Qwen router-only:            10/10; production-spine: 10/10
MiMo router-only:            10/10; production-spine: 9/10
MiMo failed case retry:       1/1
GLM router-only:             10/10; production-spine: 10/10
StepFun router-only:         10/10; production-spine: 9/10
StepFun failed case retry:    1/1
```

The MiMo and StepFun full production-spine misses were provider/CDP transient
failures that triggered the intended baseline fallback. The same failed cases
passed when rerun individually after a fresh browser session. These live runs
used the original 10-case fixture; the later project-attached chat regression
case is covered by deterministic tests and the manual harness self-tests.

## 0.3.6 Cognitive Sleep v1

Codey 0.3.6 adds an invisible local Ghost maintenance pass. It is not a
background agent, a second learning loop, or a prompt-writing system. It runs
after successful tasks, checks Ghost projection/event health, applies Hebbian
decay only when due, refreshes continuity from existing bounded local sources,
compacts event logs when existing limits require it, and writes a bounded sleep
report.

Production changes:

- New `codey/ghost/sleep.py` stores `sleep_state.json` and
  `sleep_events.jsonl`. Reports contain only cycle metadata, step names, counts,
  warnings, timings, cancellation state, and run/session/project references.
- Sleep reports do not store user task text, assistant replies, prompts,
  `Local Context`, Research bodies, webpage bodies, source snippets, or source
  code.
- Post-turn sleep is single-flight and runs in a daemon thread. It emits no SSE
  event, changes no UI state, calls no provider, opens no browser, runs no
  shell command, and generates no new memory candidates or prompt-visible free
  text.
- `ghost disable` prevents automatic sleep along with automatic Ghost learning
  and continuity sync. Existing `ghost export`, `ghost reset --yes`, and
  `ghost delete-scope` controls now cover sleep state/events.
- Hebbian decay now has a minimum maintenance interval for sleep and does not
  rewrite state/events when no weight/status change is due.
- Server and headless runner paths wait for local non-default `state_home`
  sleep completion before returning, preventing tests or CI callers from
  deleting temporary state while the maintenance thread is still writing. The
  default desktop state path remains an invisible background thread.
- No live web A/B was required because 0.3.6 does not change model-visible
  prompt text, provider adapters, or UI behavior.

Validation:

```text
python -m pytest -q
# 1686 passed, 9 skipped, 1 pytest cache warning, 264 subtests passed

python -m pytest tests\test_ghost_sleep.py tests\test_ghost_continuity.py tests\test_ghost_directive.py tests\test_ghost_learning_loop.py tests\test_ghost_inbox.py tests\test_ghost_hebbian.py tests\test_ghost_signal_extractor.py tests\test_cli.py tests\test_agent.py tests\test_server.py tests\test_permission_profiles.py tests\test_architecture.py tests\test_ui.py -q
# 479 passed, 3 skipped, 1 pytest cache warning, 116 subtests passed

python -m pytest tests\test_headless_runner.py tests\test_ghost_sleep.py tests\test_server.py -q
# 164 passed, 1 skipped, 1 pytest cache warning, 6 subtests passed

python -m ruff check .
# All checks passed!

python -B tests\manual\ghost_continuity_ab.py --self-test
# self-test ok

git diff --check
# passed
```

## 0.3.5 Ghost Continuity v1

Codey 0.3.5 adds a bounded local continuity projection. It is not a transcript
store, a second memory system, or a Research evidence layer. Runtime reads are
projection-only from `continuity.json`; rendering does not rebuild state,
quarantine files, write events, call providers, or scan project source.

Production changes:

- New `codey/ghost/continuity.py` stores `continuity.json` and
  `continuity_events.jsonl`, with export, reset, delete-scope, and explicit
  rebuild controls.
- Continuity can project accepted typed memory, recent Chat focus excerpts,
  bounded planning run-ledger facts, Research synthesis / decision titles, and
  bounded `Open questions` section lines. Raw transcripts, raw Research bodies,
  evidence sections, source snippets, webpage text, and source files are not
  rendered.
- Normal Chat and `planning_readonly` can read the neutral `Local Context`.
  Consensus sends it only to the owner prompt. Project Writer, Research,
  Reviewer, protocol repair, permissions, and tool approval paths do not receive
  continuity context.
- Task completion runs a local best-effort continuity sync after the learning
  loop. The context is eventual-consistent: newly synced continuity should be
  relied on after the post-turn `ghost_continuity_done` event, not at the exact
  instant `task_done` is emitted.
- `forget_conversation(session_id)` now best-effort deletes session-scoped
  continuity, so deleting a chat clears its recent-focus/open-question
  projection as well as the conversation store.
- Continuity event rebuild is blocked when `continuity_events.jsonl` exceeds
  the byte limit, preventing an oversized audit log from silently rebuilding an
  empty projection. Repeated sync of the same source/text is idempotent and does
  not append duplicate item events.

Validation:

```text
python -m pytest -q
# 1667 passed, 9 skipped, 1 pytest cache warning, 255 subtests passed

python -m pytest tests\test_ghost_continuity.py tests\test_ghost_directive.py tests\test_ghost_learning_loop.py tests\test_cli.py tests\test_agent.py tests\test_server.py tests\test_permission_profiles.py tests\test_architecture.py -q
# 328 passed, 3 skipped, 1 pytest cache warning, 59 subtests passed

python -m ruff check .
# All checks passed!

python -B tests\manual\ghost_continuity_ab.py --self-test
# self-test ok

git diff --check
# passed
```

Manual live A/B, restarting the dedicated 9222 Edge CDP session between
providers:

```text
python -B tests\manual\ghost_continuity_ab.py --provider deepseek --port 9222 --timeout 90 --new-chat-timeout 45 --no-open-if-missing --output tests\manual\results\ghost_continuity_deepseek.json
# ok: true
python -B tests\manual\ghost_continuity_ab.py --provider qwen --port 9222 --timeout 90 --new-chat-timeout 45 --no-open-if-missing --output tests\manual\results\ghost_continuity_qwen.json
# ok: true
python -B tests\manual\ghost_continuity_ab.py --provider mimo --port 9222 --timeout 90 --new-chat-timeout 45 --no-open-if-missing --output tests\manual\results\ghost_continuity_mimo.json
# ok: true
python -B tests\manual\ghost_continuity_ab.py --provider glm --port 9222 --timeout 90 --new-chat-timeout 45 --no-open-if-missing --output tests\manual\results\ghost_continuity_glm.json
# ok: true
python -B tests\manual\ghost_continuity_ab.py --provider stepfun --port 9222 --timeout 90 --new-chat-timeout 45 --no-open-if-missing --output tests\manual\results\ghost_continuity_stepfun.json
# ok: true
```

The live cases verified recent-focus carryover, current-request precedence over
continuity, open questions not being treated as facts, planning JSON compliance,
and no model-visible internal `Ghost` naming.

## 0.3.4 Ghost Learning Loop v1

Codey 0.3.4 closes the first Ghost learning loop for normal Chat: after
`task_done` is emitted, Codey can best-effort extract explicit learning signals
in a fresh provider tab, write the raw signal audit first, ingest inbox/gate
candidates, sync accepted typed style preferences into Hebbian state, and let
the next Chat turn pick up the resulting neutral Local Context.

Production changes:

- New `codey/ghost/typed_fields.py` is the shared typed field contract for
  extractor guidance, deterministic gate decisions, inbox conflict/value keys,
  and directive rendering. The model-visible directive still renders only known
  safe slot/value templates; unknown fields and protected topics do not become
  free text.
- New `codey/ghost/learning_loop.py` implements a synchronous post-turn,
  best-effort flow. It is provider-injected from outside `codey/ghost`, uses
  a fresh provider tab when available, writes `signals.jsonl` before inbox or
  Hebbian state, closes the learning provider, and returns bounded diagnostics.
- Normal Chat triggers learning only after the user-facing `task_done` event.
  `planning_readonly` has code coverage but is not enabled by default. Project
  Writer, Research, Reviewer, protocol repair prompts, tool permissions, and
  command execution do not receive automatic learning.
- `ghost disable` prevents post-turn extractor calls while keeping local
  controls such as list/export/directive/reset/delete-scope available.
- Auto-accept is stricter than the inbox-only release: high-confidence
  `style_preference` signals must include grounded known typed metadata before
  they can be accepted and reinforced. Unknown style fields remain candidates;
  `correction` and `action_tendency` are not automatically reinforced.
- Post-review hardening keeps automatic learning out of inbox/Hebbian when the
  extractor result has diagnostics, even if schema parsing recovered partial
  valid signals. The raw `signals.jsonl` audit row is still written so the
  failure is inspectable.
- Typed field safety is pair-based: directive rendering and gate auto-accept
  require an explicit kind/slot/value entry such as
  `style_preference/reply_length/concise`. Known slots and known values are not
  cross-combined, so `format=concise`, `tone=table`, and hidden aliases like
  `style_preference:length` stay non-renderable and cannot be auto-accepted.
- Added `tests/manual/ghost_learning_loop_ab.py`, a one-provider-at-a-time live
  A/B probe that checks fresh-tab extraction, learned directive context, answer
  style change, negative no-signal behavior, and internal naming leakage.
  The live harness opens the extractor in a temporary sibling tab from the same
  provider browser context to avoid nested Playwright sync attachments while
  still keeping the extractor prompt out of the user's current chat tab.

Validation:

```text
python -B -m py_compile codey\ghost\typed_fields.py codey\ghost\directive.py codey\ghost\gate.py codey\ghost\inbox.py codey\ghost\signal_codec.py codey\ghost\learning_loop.py codey\task_runner.py codey\server.py
# passed

python -m pytest tests\test_ghost_learning_loop.py tests\test_ghost_directive.py tests\test_ghost_inbox.py tests\test_ghost_hebbian.py tests\test_ghost_signal_extractor.py tests\test_cli.py tests\test_server.py tests\test_architecture.py -q
# 275 passed, 1 skipped, 1 pytest cache warning, 83 subtests passed in 103.17s

python -m ruff check .
# All checks passed!

python -B tests\manual\ghost_learning_loop_ab.py --self-test
# self-test ok

python -m pytest -q
# 1648 passed, 9 skipped, 1 pytest cache warning, 241 subtests passed in 357.15s
```

Manual live A/B, restarting the dedicated 9222 Edge CDP session between
providers:

```text
python -B tests\manual\ghost_learning_loop_ab.py --provider deepseek --port 9222 --timeout 90 --new-chat-timeout 45 --output tests\manual\results\ghost_learning_loop_deepseek.json
# ok: true
python -B tests\manual\ghost_learning_loop_ab.py --provider mimo --port 9222 --timeout 90 --new-chat-timeout 45 --output tests\manual\results\ghost_learning_loop_mimo.json
# ok: true
python -B tests\manual\ghost_learning_loop_ab.py --provider qwen --port 9222 --timeout 90 --new-chat-timeout 45 --output tests\manual\results\ghost_learning_loop_qwen.json
# ok: true
python -B tests\manual\ghost_learning_loop_ab.py --provider glm --port 9222 --timeout 90 --new-chat-timeout 45 --output tests\manual\results\ghost_learning_loop_glm.json
# ok: true
python -B tests\manual\ghost_learning_loop_ab.py --provider stepfun --port 9222 --timeout 90 --new-chat-timeout 45 --output tests\manual\results\ghost_learning_loop_stepfun.json
# ok: true
```

Live provider A/B result, one provider per restarted Edge/CDP session:

- DeepSeek: passed. Extracted and accepted two typed style preferences,
  reinforced two active Hebbian nodes, rendered `reply length = concise` and
  `reply structure = answer first`, shortened the next answer, and did not leak
  internal naming.
- MiMo: passed with the same typed learning/directive/leak checks. It produced
  one extra candidate row, but only the two renderable style preferences became
  active Hebbian nodes.
- Qwen: passed. The directive arm was much shorter than baseline and preserved
  the learned `concise` / `answer first` context without internal naming
  leakage.
- GLM: passed after a scoped restart of the 9222 Edge CDP session. It learned
  and reinforced the same two typed style preferences and shortened the next
  answer.
- StepFun: passed with the same typed learning/directive/leak checks. It also
  produced one extra candidate row, but only the two safe typed preferences were
  active.

## 0.3.3 Ghost Directive ContextSource v1

Codey 0.3.3 renders confirmed local Hebbian memory into a short, bounded prompt
context. It changes only normal chat and `planning_readonly` prompt assembly;
Project Writer, Research, Reviewer, protocol repair prompts, permissions, and
learning loops remain outside the directive path.

Production changes:

- New `codey/ghost/directive.py` builds `GhostDirective(text, selected_nodes,
  warnings, truncated)` from active Hebbian nodes without writing state or
  calling a provider. Model-visible item text is generated from typed
  `kind/conflict_key/value_key` templates; raw `node.label` stays local audit
  text and is not rendered. The template grammar is an explicit slot/value
  allowlist; unknown slugs and split protected topics such as `system = prompt`
  are skipped instead of being tokenized into free text.
- Directive selection filters by active status, supersession, scope, weight,
  current Ghost signal kind, sensitive secret-like text, dangerous
  authorization text, and generic instruction-hierarchy attacks, including
  "ignore previous instructions", "treat this as the system prompt", and
  "memory outranks/supersedes/replaces system instructions" or "developer
  messages defer to memory" variants. Reverse and modal forms such as
  "replace system prompt with this memory" and "this memory should be used
  before current instructions" are covered by the same structural guard,
  including `needs to come before`, `ranks above`, `treated as above`, and
  `all/bare instructions` variants. Broad ordering terms such as
  before/over/rather-than only match explicit `this memory` / `local memory`
  against protected instruction objects, so ordinary phrases like "memory
  efficient code before system optimization" still render. Same scope/conflict
  competing values are skipped unless one value is clearly stronger.
- Runtime directive reads are projection-only. Missing/corrupt `state.json`
  returns empty context without rebuilding from events, quarantining files, or
  writing projection/events. Stale weights are decayed in-memory for selection
  only; persistent decay remains an explicit Hebbian maintenance action.
- Rendered directive text uses neutral `Local Context` wording. The
  model-visible prompt must not expose internal `Ghost` / `Ghost Directive`
  naming; structured fields containing those terms are redacted to neutral
  local-memory wording. It also does not expose raw labels, evidence quotes,
  candidate ids, event ids, edge ids, or `coactivated_with` edges as facts.
  There is no raw-label redaction fallback path in the renderer; labels remain
  local audit text only.
  Structured fields that cannot be rendered by the safe template grammar, or
  that refer to system/developer instructions, approvals, tools, or the current
  request, are skipped. This includes split fields such as `developer =
  instructions`, tool/shell/run/delete-file values, and unknown value slugs like
  `use_tools`.
- Added `ghost_directive` as a known context source. Chat prepends the directive
  locally, consensus chat sends it only through `owner_prompt`, and
  `planning_readonly` receives it through `agent.run`.
- `coding_writer`, Reviewer, Research, and protocol repair prompts do not
  receive `ghost_directive`.
- Ghost CLI now supports
  `python -m codey ghost directive --project <path> --session-id <id> --budget 900`.
- Added `tests/manual/ghost_directive_ab.py` as a one-provider-at-a-time manual
  A/B probe. It has a self-test and writes bounded JSON failure rows.

Validation:

```text
$env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP 'codey-pycache'
python -B -m py_compile codey\ghost\directive.py codey\ghost\__init__.py codey\cli.py codey\permission_profiles.py codey\agent.py codey\task_runner.py codey\__init__.py tests\test_ghost_directive.py tests\test_cli.py tests\test_permission_profiles.py tests\test_agent.py tests\test_server.py tests\manual\ghost_directive_ab.py
# passed

python -m pytest tests\test_ghost_directive.py tests\test_cli.py tests\test_agent.py tests\test_server.py tests\test_permission_profiles.py -q
# 291 passed, 3 skipped, 1 pytest cache warning, 41 subtests passed in 167.48s

python -m ruff check .
# All checks passed!

python -B tests\manual\ghost_directive_ab.py --self-test
# self-test ok

python -m pytest -q
# 1633 passed, 9 skipped, 1 pytest cache warning, 231 subtests passed in 429.12s
```

Manual live A/B:

```text
python -B tests\manual\ghost_directive_ab.py --provider deepseek --port 9222 --timeout 90 --new-chat-timeout 45 --output tests\manual\results\ghost_directive_deepseek.json
# ok: true
python -B tests\manual\ghost_directive_ab.py --provider mimo --port 9222 --timeout 90 --new-chat-timeout 45 --output tests\manual\results\ghost_directive_mimo.json
# ok: true
python -B tests\manual\ghost_directive_ab.py --provider qwen --port 9222 --timeout 90 --new-chat-timeout 45 --output tests\manual\results\ghost_directive_qwen.json
# ok: true
python -B tests\manual\ghost_directive_ab.py --provider glm --port 9222 --timeout 90 --new-chat-timeout 45 --output tests\manual\results\ghost_directive_glm.json
# ok: true
python -B tests\manual\ghost_directive_ab.py --provider stepfun --port 9222 --timeout 90 --new-chat-timeout 45 --output tests\manual\results\ghost_directive_stepfun.json
# ok: true
```

Live provider A/B result, one provider per process:

- DeepSeek: passed. Baseline answered SQLite; directive answered
  "bounded local JSON projection plus JSONL audit". No internal context naming
  leaked, and planning JSON stayed valid.
- MiMo: passed. Baseline asked for more context; directive answered
  "bounded local JSON projection plus a JSONL audit log". No internal context
  naming leaked, and planning JSON stayed valid.
- Qwen: passed. Baseline answered unrelated JVM/stream-processing memory;
  directive answered "Bounded local JSON projection plus JSONL audit". No
  internal context naming leaked, and planning JSON stayed valid.
- GLM: passed after restarting a half-stale 9222 CDP browser. Baseline answered
  ephemeral in-memory state; directive answered "bounded local JSON projection
  plus JSONL audit". No internal context naming leaked, and planning JSON stayed
  valid.
- StepFun: passed. Baseline said implementation details were not public;
  directive answered bounded JSON projection plus JSONL audit. No internal
  context naming leaked. Planning JSON stayed valid; StepFun wrapped the JSON in
  a code-block-style UI copy prefix, which the existing parser accepted.

- Project Writer and Research remain covered by deterministic tests only in
  this release; they do not receive directive context.

## 0.3.2 Ghost Hebbian State v1

Codey 0.3.2 adds an auditable local memory weight ledger for accepted Ghost
inbox candidates. It still does not inject Ghost state into prompts, wire Ghost
into TaskRunner, add UI, run automatic daily learning, or change chat/coding/
Research behavior.

Production changes:

- New `codey/ghost/hebbian.py` stores bounded `GhostNode` and
  `coactivated_with` `GhostEdge` rows in `state_home/ghost/state.json`, with
  `state_home/ghost/hebbian_events.jsonl` as the separate audit/replay log.
- Inbox candidates now include `value_key`, `evidence_refs`, review metadata,
  and `superseded_by`. Same scope/ref/conflict/value rows merge evidence;
  competing values stay separate.
- Later candidate/rejected ingest can no longer downgrade an accepted
  candidate. Manual `accept` can supersede older accepted values for the same
  scope and conflict key, ordinary ingest cannot revive a superseded value, and
  ordinary ingest preserves manual review metadata.
- Hebbian reinforcement is deterministic and local: bounded weight updates,
  evidence-ref dedupe, continuous and idempotent half-life decay using `ln(2)`,
  pair/run-scoped coactivation evidence, edge fanout caps, scope filtering,
  export, reset, delete-scope, and event replay.
- Bad Hebbian projections are quarantined, bad event lines are skipped with
  warnings, oversized event logs block rebuild instead of overwriting state,
  and projection write failures fail open.
- Ghost CLI now supports
  `list/export/accept/reject/state/rebuild-state/reset/delete-scope/enable/disable`.
  `export`, `reset`, and `delete-scope` include raw signals, inbox/events, and
  Hebbian state/events. `accept` can backfill same-run coactivation edges, and
  `reject` removes the corresponding Hebbian node and connected edges.
- `sync_from_inbox()` reconciles rejected and superseded inbox rows instead of
  only reinforcing accepted rows.
- Hebbian node kinds are limited to the current five Ghost signal kinds until
  future extractor/gate paths exist.
- `server.State` creates `ghost_hebbian` only when `state_home` exists; bare
  `State()` disables Ghost writes.

Validation:

```text
python -B -m py_compile codey\ghost\inbox.py codey\ghost\hebbian.py codey\ghost\gate.py codey\cli.py codey\server.py codey\ghost\__init__.py codey\__init__.py tests\test_ghost_inbox.py tests\test_ghost_hebbian.py
# passed

python -B -m unittest tests.test_ghost_hebbian tests.test_ghost_inbox
# Ran 61 tests in 4.343s
# OK

python -m pytest tests\test_ghost_inbox.py tests\test_ghost_hebbian.py tests\test_ghost_signal_extractor.py tests\test_cli.py tests\test_architecture.py tests\test_server.py -q
# 232 passed, 1 skipped, 1 pytest cache warning, 40 subtests passed in 155.47s

python -m ruff check .
# All checks passed!

python -m pytest -q
# 1602 passed, 9 skipped, 1 pytest cache warning, 198 subtests passed in 404.66s
```

## 0.3.1 Ghost Memory Inbox v1

Codey 0.3.1 adds the local Ghost memory inbox and deterministic gate. It keeps
Ghost candidates auditable and user-controllable without injecting prompt
context, updating Hebbian state, or changing chat/coding/Research behavior.

Production changes:

- New `codey/ghost/inbox.py` projects 0.3.0 `GhostSignal` values into bounded
  `GhostMemoryCandidate` rows with status, scope, evidence quote, confidence,
  provenance, conflict key, gate reason, metadata, and reinforcement count.
- New `codey/ghost/gate.py` performs local-only safety and quality checks.
  High-confidence style preferences can be marked `accepted`; corrections,
  research interests, long-term goals, and action tendencies remain candidates
  by default. Correction auto-accept and conflict grouping do not use
  hard-coded Chinese/English phrase lists.
- `state_home/ghost/events.jsonl` is the inbox/gate/control source of truth.
  `state_home/ghost/inbox.json` is a rebuildable projection, and
  `state_home/ghost/settings.json` stores learning enablement. 0.3.1 does not
  write `state.json`.
- `events.jsonl` compacts by both event count and byte size. Oversized event
  logs produce an `events_too_large` warning and do not cause Codey to rewrite
  a missing/bad projection as empty or overwrite it with only new candidates.
- Bad projection JSON and future projection/settings schemas are quarantined;
  bad event lines are skipped with bounded warnings. Projection write failures
  are fail-open and can rebuild from events.
- Sensitive rejected signals write only sanitized rejection events and do not
  enter the active inbox projection.
- `delete-scope` and `reset` physically compact or remove active Ghost store
  content and raw `signals.jsonl` audit entries instead of preserving deleted
  text behind tombstones.
- New local CLI controls:
  `python -m codey ghost list/export/reset/delete-scope/enable/disable`.
  These commands do not load provider/browser/tool runtime modules. CLI storage
  failures return bounded JSON instead of tracebacks.
- `server.State` creates `ghost_inbox` only when `state_home` is present; bare
  `State()` disables Ghost writes.

Validation:

```text
python -m ruff check .
# All checks passed!

python -B -m unittest tests.test_ghost_inbox
# 31 passed

python -B -m unittest tests.test_ghost_signal_extractor
# 21 passed

python -B -m unittest tests.test_cli
# 9 passed

python -B -m unittest tests.test_architecture
# 5 passed

python -B -m unittest tests.test_server
# 137 passed, 1 skipped

python -B -m unittest tests.test_ghost_inbox tests.test_ghost_signal_extractor tests.test_cli tests.test_architecture tests.test_server
# 203 passed, 1 skipped

python -m pytest
# 1572 passed, 9 skipped, 1 pytest cache warning, 196 subtests passed
```

## 0.3.0 Ghost Signal Extractor v1

Codey 0.3.0 starts the Ghost line with a narrow explicit-signal extractor. It
does not add accepted memory, Hebbian updates, prompt directives, UI, or
production behavior changes.

Production changes:

- New `codey/ghost/` package with schema, JSON signal codec, fail-open provider
  extractor, and append-only candidate event store.
- `GhostSignalCodec` accepts only one JSON object with a `signals` list. Valid
  candidates are bounded and limited to `style_preference`, `correction`,
  `research_interest`, `long_term_goal`, and `action_tendency`.
- `evidence_quote` is validated against the current user message; invented
  quotes are rejected. No-signal replies use an empty `signals` list. Signals
  that look like passwords, API keys, bearer tokens, private keys, or
  high-entropy secrets are rejected before they can be written to disk.
- `GhostSignalExtractor` is manual/shadow oriented. Provider failures return no
  signals and do not affect chat, coding, Research, TaskRunner, or repair
  prompts.
- `GhostSignalStore` writes bounded candidate extraction events to
  `state_home/ghost/signals.jsonl` and stores no full user/assistant transcript.
  `State(state_home=None)` disables Ghost writes. Importing the store/schema
  path stays lightweight and does not load provider/browser adapters.
- Added `tests/manual/ghost_signal_extractor_ab.py` for one-provider-at-a-time
  live A/B. The baseline arm emits no signals; the extractor arm calls the
  provider. Its self-test uses a fake provider and does not open browser tabs.
  Provider/CDP connection failures are written as bounded failure rows instead
  of masking the original error with a probe exception.
- Live A/B also exposed a CDP lifecycle issue in the manual path: skipping
  `Session.close()` with `--keep-open` can leave Playwright/CDP automation in a
  half-stale state, while production Codey normally closes provider sessions.
  The Ghost probe now always closes non-isolated automation, and failed
  non-isolated browser launches clean up the child process. Codey deliberately
  does not silently switch to a different CDP port after Playwright attach
  failure, because the opened port may carry the user's logged-in provider
  tabs.
- Architecture guard: the Ghost package imports neither `torch` nor
  `transformers`.

Validation:

```text
python -m pytest tests\test_ghost_signal_extractor.py -q
# 21 passed, 1 pytest cache warning, 5 subtests passed

python -B tests\manual\ghost_signal_extractor_ab.py --self-test
# self-test ok

python -B tests\manual\ghost_signal_extractor_ab.py --provider deepseek --port 9222 --timeout 120 --new-chat-timeout 60 --output tests\manual\results\ghost_signal_extractor_deepseek.json
# extractor 7/7, explicit 5/5, no-signal 2/2, grounded 7/7; baseline 2/7

python -B tests\manual\ghost_signal_extractor_ab.py --provider qwen --port 9222 --timeout 120 --new-chat-timeout 60 --output tests\manual\results\ghost_signal_extractor_qwen.json
# extractor 7/7, explicit 5/5, no-signal 2/2, grounded 7/7; baseline 2/7

python -B tests\manual\ghost_signal_extractor_ab.py --provider mimo --port 9222 --timeout 120 --new-chat-timeout 60 --output tests\manual\results\ghost_signal_extractor_mimo.json
# extractor 7/7, explicit 5/5, no-signal 2/2, grounded 7/7; baseline 2/7

python -B tests\manual\ghost_signal_extractor_ab.py --provider stepfun --port 9222 --timeout 120 --new-chat-timeout 60 --output tests\manual\results\ghost_signal_extractor_stepfun.json
# extractor 7/7, explicit 5/5, no-signal 2/2, grounded 7/7; baseline 2/7

python -B tests\manual\ghost_signal_extractor_ab.py --provider glm --port 9222 --timeout 120 --new-chat-timeout 60 --output tests\manual\results\ghost_signal_extractor_glm.json
# extractor 7/7, explicit 5/5, no-signal 2/2, grounded 7/7; baseline 2/7

Live A/B note: DeepSeek and Qwen initially exposed two classification-boundary
issues. The prompt was tightened so communication format/tone preferences map
to `style_preference`, workflow/process preferences map to `action_tendency`,
and internal product names are not shown in the model-visible extractor prompt.
After the prompt fix, all five web providers passed the same seven-case probe.

python -m py_compile codey\ghost\schema.py codey\ghost\signal_codec.py codey\ghost\extractor.py codey\ghost\store.py
# passed

python -m pytest tests\test_browser.py tests\test_ghost_signal_extractor.py -q
# 65 passed, 1 pytest cache warning, 5 subtests passed

python -m pytest tests\test_ghost_signal_extractor.py tests\test_architecture.py tests\test_server.py -q
# 162 passed, 1 skipped, 1 pytest cache warning, 8 subtests passed

python -m ruff check codey tests
# All checks passed

python -m pytest -q
# 1541 passed, 9 skipped, 1 pytest cache warning, 166 subtests passed

git diff --check
# passed
```

## 0.2.33 Project-local Config v1

Codey 0.2.33 adds a strict project-local config parser for explicit
`.codey/config.json` files. The config is a bounded project fact/preference
source; it does not authorize tools or relax runtime safety gates.

Production changes:

- New `codey/project_config.py` parses schema v1 config files with a 64 KiB
  file cap, bounded warnings, regular-file checks, and project-relative path
  validation. Oversized config files are rejected from `stat().st_size` before
  reading the body.
- Configured verification commands feed `discover_verification_candidates()`
  with a stable source priority below previously successful checks and above
  manifest discovery. They still must pass executable availability,
  cwd-in-project validation, and the existing `tool_runtime` run allowlist.
- `VerificationCandidate` now carries `source_priority`; selected verification
  remains deterministic and historical green checks keep the highest priority.
- `scan.ignored_paths` uses project-root-relative prefix matching and is applied
  to Project Map listing, symbol overview, focused subtree, and verification
  discovery. Existing hidden, secret, symlink, and default excluded-path guards
  remain in force.
- `context.budget_hints.project_map_chars` can only reduce the Project Map
  render budget and has a lower bound. It cannot increase prompt budgets.
- Project config warnings are rendered as a small ContextSource block for
  Project Writer and read-only planning prompts. Protocol repair prompts do not
  include project config context.
- Provider preferences are parsed and validated as future hints only; this
  release does not consume them for provider selection or failover. Validation
  uses the lightweight static provider capability table, not the web adapter
  registry.
- Headless JSONL runs naturally reuse the config because they already flow
  through `TaskRunner` and `ProjectTaskContextBuilder`.
- Live web-provider smoke hardening: StepFun now refills its composer until
  the submitted text survives late page hydration, then lets submission report
  missing send controls as send-button failures; the manual submit probe closes
  reused Playwright CDP sessions even when `--keep-open` is set;
  `tools/live_smoke.py --provider all` now targets web providers only and
  excludes `local`. StepFun no longer keeps an unreachable Enter fallback
  behind the stable composer gate.
- This release does not add a workflow DSL, shell auto-approval,
  project-local permission matrix, automatic config writing, Research headless
  config, UI changes, or any relaxation of runtime guards.

Validation:

```text
python -m pytest tests\test_project_config.py tests\test_verification_policy.py tests\test_project_map.py tests\test_project_task_context.py tests\test_agent.py tests\test_headless_runner.py tests\test_permission_profiles.py -q
# 208 passed, 4 skipped, 1 pytest cache warning, 15 subtests passed

python -m pytest tests\test_project_config.py tests\test_verification_policy.py tests\test_project_map.py tests\test_project_task_context.py tests\test_agent.py tests\test_headless_runner.py tests\test_permission_profiles.py tests\test_server.py tests\test_run_ledger.py -q
# 353 passed, 5 skipped, 1 pytest cache warning, 15 subtests passed

python -m pytest tests\test_task_runner_project_map.py tests\test_project_task_context.py tests\test_project_map.py -q
# 39 passed, 1 pytest cache warning

python -m pytest tests\test_stepfun.py tests\test_provider_submit_probe.py tests\test_live_smoke.py tests\test_deepseek.py tests\test_mimo.py tests\test_qwen.py tests\test_glm.py -q
# 183 passed, 1 pytest cache warning, 11 subtests passed

python -m pytest tests\test_project_config.py tests\test_project_task_context.py tests\test_verification_policy.py tests\test_project_map.py tests\test_stepfun.py tests\test_provider_submit_probe.py tests\test_live_smoke.py -q
# 126 passed, 2 skipped, 1 pytest cache warning, 5 subtests passed

python -B tools\live_smoke.py --provider deepseek --case discussion --port 9222 --max-turns 4 --json
# ok=true, stop_reason=done, turns=1, changed=false

python -B tools\live_smoke.py --provider mimo --case discussion --port 9222 --max-turns 4 --json
# ok=true, stop_reason=done, turns=1, changed=false

python -B tools\live_smoke.py --provider stepfun --case discussion --port 9222 --max-turns 4 --json
# ok=true, stop_reason=done, turns=1, changed=false

python -B tools\live_smoke.py --provider qwen --case discussion --port 9222 --max-turns 4 --json
# ok=true, stop_reason=done, turns=1, changed=false

python -B tools\live_smoke.py --provider glm --case discussion --port 9222 --max-turns 4 --json
# ok=true, stop_reason=done, turns=1, changed=false

python -m pytest -q
# 1518 passed, 9 skipped, 1 pytest cache warning, 161 subtests passed
```

## 0.2.32 Headless JSONL Runner v1

Codey 0.2.32 adds a TaskRunner-backed headless JSONL path for scripts, CI
smoke checks, and future Ghost/automation callers. It migrates
`python -m codey agent --json` onto the production orchestration spine without
adding a second agent loop.

Production changes:

- New `codey/headless_runner.py` defines `HeadlessRequest`,
  `HeadlessResult`, `HeadlessState`, `run_headless()`, and bounded JSONL event
  projection helpers.
- `HeadlessState` reuses `server.State` and overrides only `get_provider()` and
  `emit()`. It keeps `reserve_run`, `start_run`, `finish_run`,
  `change_tracker_for`, Run Ledger, Managed Outputs, and provider supervision
  on the same path as the UI.
- `python -m codey agent --json` now calls `run_headless()`. Plain
  `python -m codey agent` remains on the existing direct path for this release.
- Headless JSONL emits bounded task, status/info, turn, tool, shell rejection,
  and task completion rows. Full model replies, full command output, and
  UI-only state are not dumped.
- `--readonly` maps to the internal `planning_readonly` profile. The
  TaskRunner now has an explicit `planning_readonly` task kind, projected as
  `planning` in terminal events. It does not collect diffs, create Work
  Checkpoints, run Review, or write ProjectFacts.
- Project coding headless runs reuse Run Ledger, Managed Outputs, provider
  fallback ordering, change tracking, and receipt generation. The first
  headless version uses a no-op review callback rather than silently opening a
  reviewer model.
- Headless shell requests are default-deny: `shell_request` emits
  `shell_rejected` with `headless_default_deny`, does not approve the command,
  and returns a non-zero exit.
- Review hardening: a caller-supplied `HeadlessRequest.run_id` is now
  pre-reserved in `HeadlessState` before entering `TaskRunner`, so it cannot
  produce an empty no-op run. Terminal `task_done` JSONL now includes
  `ledger_path` when a ledger was written and a stable `mode` matching
  `task_start`.
- Removed the unused legacy CLI JSONL helper now that `agent --json` delegates
  to `headless_runner.emit_jsonl()`.
- The stale browser attach-only test mock was updated to patch the current
  `_ensure_cdp_endpoint()` seam instead of depending on a real CDP browser
  being open.

Validation:

```text
python -m pytest tests\test_headless_runner.py tests\test_cli.py tests\test_browser.py tests\test_agent.py tests\test_server.py tests\test_run_ledger.py -q
# 309 passed, 3 skipped, 1 pytest cache warning

python -m ruff check codey tests
# All checks passed

python -m py_compile codey\headless_runner.py codey\cli.py codey\task_runner.py codey\server.py codey\agent.py codey\__init__.py
# passed

python -m pytest -q
# 1491 passed, 8 skipped, 1 pytest cache warning, 161 subtests passed
```

## 0.2.31 Internal Permission Profiles v1

Codey 0.2.31 adds internal permission profiles for runtime phase boundaries.
It names and tests existing tool/context boundaries without adding a
user-visible mode switch or replacing runtime safety gates.

Production changes:

- New `codey/permission_profiles.py` defines internal `chat`, `research`,
  `coding_writer`, `reviewer`, and `planning_readonly` profiles.
- `ToolDefinition` can now render contracts from filtered definition sets.
- `JsonToolCodec()` still defaults to the full Project Writer contract, while
  `JsonToolCodec(permission_profile="planning_readonly")` omits `edit`, `run`,
  and `shell`.
- Coding protocol errors now distinguish `unknown_tool` from `disallowed_tool`.
  `write_file` remains unknown; `edit` in `planning_readonly` is disallowed.
- `parallel` checks both `parallel_safe` and the active profile.
- Empty coding tool sets no longer render the full writer contract, and
  non-coding profiles fail fast if used to construct a coding codec.
- Tests lock `coding_writer` to every current `ToolDefinition`, so future tool
  permissions cannot silently split prompt rendering from parsing.
- `agent.run()` uses `permission_profile` for default codec creation and
  ContextSource filtering, while respecting explicit codecs.
- `consensus.READ_ONLY_CODEC` now uses the `planning_readonly` profile. Project
  Writer calls are explicitly bound to `coding_writer`; Research/Reviewer
  profiles are declared and tested without rewriting those runtimes.

Validation:

```text
python -m pytest tests\test_permission_profiles.py tests\test_tool_definition.py tests\test_protocols.py tests\test_agent.py tests\test_consensus.py tests\test_server.py tests\test_research.py -q
# 408 passed, 4 skipped, 1 pytest cache warning, 60 subtests passed

python -m ruff check codey tests
# All checks passed

python -m py_compile codey\permission_profiles.py codey\tool_definition.py codey\protocols\json_codec.py codey\protocols\base.py codey\agent.py codey\consensus.py codey\research\runner.py codey\task_runner.py codey\__init__.py
# passed

python -m pytest -q
# 1485 passed, 8 skipped, 1 pytest cache warning, 161 subtests passed

git diff --check
# passed, with tests/test_consensus.py CRLF normalization warning
```

## 0.2.30 Managed Output Handles v1

Codey 0.2.30 adds run-scoped managed output handles for truncated Project
Writer `run` output. It keeps model-facing tool results bounded while retaining
the raw command output locally for future audit/export/debug paths.

Production changes:

- New `codey/managed_outputs.py` defines `ManagedOutputStore`,
  `ManagedOutputRef`, and `run_command_with_managed_output()`.
- `tool_runtime.run_command()` now uses an internal raw/projection split. The
  default public behavior remains the same; managed Project Writer runs can
  save raw stdout/stderr before dependency stack pruning and prompt clipping.
- Handles are written only when the projected `run` result is truncated. Short
  command output is not stored.
- Managed output metadata records the production `tool_id`, `original_bytes`,
  `stored_bytes`, stored-text `sha256`, and `stored_truncated`. Single outputs
  and per-run handle counts are capped, paths are constrained under
  `state_home/managed_outputs`, and write failures fail open. Outputs that
  exceed the stored byte cap keep head and tail text with an omission marker.
- `ToolOutcome` and `ToolResult` carry optional handle metadata. `JsonToolCodec`
  renders a short model-visible footer that says the handle is for local
  audit/export, not a tool. Full output is not injected into prompts.
- Run Ledger `tool_finished` events record handle id, original/stored byte
  counts, and stored-output hash without saving full command output.
- `State()` enables managed outputs only when `state_home` is provided. Bare
  test/embedding state keeps `managed_outputs=None`.

Validation:

```text
python -m pytest tests\test_managed_outputs.py tests\test_tool_runtime.py tests\test_protocols.py tests\test_run_ledger.py tests\test_server.py -q
# 256 passed, 4 skipped, 1 pytest cache warning, 27 subtests passed

python -m pytest tests\test_managed_outputs.py tests\test_tool_runtime.py tests\test_protocols.py tests\test_run_ledger.py tests\test_server.py tests\test_agent.py tests\test_agent_tools.py -q
# 363 passed, 6 skipped, 1 pytest cache warning, 27 subtests passed

python -m unittest tests.test_work_checkpoint_flow
# 15 passed

python -m pytest -q
# 1467 passed, 8 skipped, 1 pytest cache warning, 145 subtests passed

python -m ruff check codey tests
# All checks passed

python -m py_compile codey\managed_outputs.py codey\agent_tools.py codey\agent.py codey\task_runner.py codey\tool_runtime.py codey\models.py codey\protocols\json_codec.py codey\run_ledger.py codey\server.py codey\__init__.py
# passed
```

## 0.2.29 Provider Capability Registry v1

Codey 0.2.29 adds a static provider capability registry for conservative
fallback ordering. It does not add provider ranking UI, runtime capability
learning, model self-routing, or Research mid-run failover.

Production changes:

- New `codey/provider_capabilities.py` defines `ProviderCapability`,
  `capability_for()`, and `rank_providers()`.
- Capabilities describe static hints only: JSON reliability, coding/research/
  review fit, context budget hint, native-tool interference risk, canary hint,
  failure families, and notes.
- `rank_providers()` is pure and deterministic. It preserves input order as the
  tie-breaker, keeps explicit `preferred` providers first, treats `avoid` as
  "rank later" rather than "disable", honors `excluded`, and returns defaults
  for unknown providers. Generic hybrid ranking uses the stricter of Research
  and Coding fit.
- `TaskRunner` consumes capability ordering only on replacement paths:
  selected provider unavailable, connect failure, canary failure, and Writer
  failover. Hybrid startup fallback is ranked as Research because the first
  phase runs Research; hybrid Writer failover is ranked as Project.
  User-selected providers are not preempted while available.
- `reviewer_candidates()` runs candidates through review-mode static ordering
  while still filtering writer/local/unavailable providers and keeping UI
  payloads free of capability fields.
- `ProviderSupervisor` remains the runtime health/cooldown/canary owner.
  Runtime failures do not mutate static capabilities and capabilities are not
  stored in `provider-health.json`.
- `failure_families` are tested against the real `ProviderFailure` kind
  vocabulary. `context_budget_hint` is not consumed by production policy in
  this release.

Validation:

```text
python -m unittest tests.test_provider_capabilities tests.test_provider_supervisor tests.test_writer_failover
# 32 passed

python -m unittest tests.test_work_checkpoint_flow
# 15 passed

python -m pytest tests\test_server.py -q
# 135 passed, 1 skipped, 1 pytest cache warning

python -m pytest -q
# 1455 passed, 8 skipped, 1 pytest cache warning, 145 subtests passed

python -m ruff check codey tests
# All checks passed

python -m py_compile codey\provider_capabilities.py codey\task_runner.py codey\server.py codey\__init__.py
# passed
```

## 0.2.28 ContextSource v1

Codey 0.2.28 adds a small named context assembly layer for project Writer
prompts. It keeps business loading in the existing stores/builders and makes
prompt context blocks explicit, bounded, fail-open where appropriate, and
testable.

Production changes:

- New `codey/context_source.py` defines `ContextSource`,
  `RenderedContextSource`, `render_context_source()`, and
  `render_context_sources()`.
- `agent.py` now renders project instructions, verified project facts, Research
  Brief, Project Map, Work Checkpoint, and initial listing through
  `ContextSource`.
- `ProjectTaskContextBuilder` still owns loading verified facts, knowledge,
  maps, checkpoints, and verification candidates. `ContextSource` does not
  import or depend on those stores.
- `Coding current local context` is rendered through `ContextSource` only in
  the post-tool-result prompt path. It still does not enter protocol repair
  prompts.
- Optional source failures fail open, but `TaskCancelled` and
  `DeadlineExceeded` are re-raised so Stop and provider deadlines are not
  swallowed during prompt assembly.
- Work Checkpoint context budget is derived from producer limits in
  `work_checkpoint.py`; tests lock that bounded rendered checkpoints fit the
  source budget and keep their changed-file list.
- Source metadata is retained for code/tests/future projections and is not
  rendered into model-visible prompts.

Validation:

```text
python -m unittest tests.test_work_checkpoint tests.test_context_source tests.test_agent tests.test_project_task_context tests.test_coding_context
# 136 tests passed, 2 skipped

python -m unittest tests.test_work_checkpoint_flow
# 15 passed

python -m pytest tests\test_server.py -q
# 128 passed, 1 skipped, 1 pytest cache warning

python -m pytest -q
# 1439 passed, 8 skipped, 1 pytest cache warning, 133 subtests passed

python -m ruff check codey tests
# All checks passed

python -m py_compile codey\context_source.py codey\agent.py codey\work_checkpoint.py codey\project_task_context.py codey\coding_context.py codey\__init__.py
# passed
```

## 0.2.27 ToolDefinition v1

Codey 0.2.27 extracts the existing coding tool metadata out of
`codey/protocols/json_codec.py` and into `codey/tool_definition.py`. This is a
small architecture refactor, not a new tool system: public JSON tool names,
runtime tool names, schema validation, dispatch, read-before-edit, shell
approval, and the run allowlist remain unchanged.

0.2.26 proof boundary:

- `run_ledger_projection.py` is still a pure read model.
- The only production projection consumer remains the terminal receipt
  shadow-consume path in `TaskRunner._event_with_projected_receipt()`.
- `WorkCheckpointStore`, checkpoint prompt rendering, checkpoint resume, and
  restore paths still use the existing checkpoint/change tracker systems. The
  0.2.26 projection tests prove conservative receipt adoption, not ledger-based
  checkpoint recovery.

Production changes:

- New `codey/tool_definition.py` defines `ToolDefinition` and the only coding
  tool metadata table. It covers the existing tools: `list_dir`, `read_file`,
  `read_files`, `grep`, `find_references`, `parallel`, `edit`, `run`, `shell`,
  and `done`.
- `JsonToolCodec` now consumes the definition layer for the prompt contract,
  aliases, parallel-safety checks, result tool names, and batch limits. It no
  longer owns or re-exports the tool definition table.
- `agent.py` derives supported runtime tools, information follow-up tools,
  repair examples, and tool activity rows from the definition layer.
- `edit` declares `file_changed` and `run` declares `command_verified`, the
  only extra Run Ledger v1 facts currently declared by tool definitions. Tests
  assert those declarations match Run Ledger events. `write` and `write_file`
  remain unknown tools.
- Shell tool-start activity now renders as `Requesting shell approval for ...`;
  tests lock that intentional visible wording change.
- Existing schema validation stays in `JsonToolCodec._tool_call()`, and runtime
  safety stays in `tool_runtime.py`.

Validation:

```text
python -m pytest tests\test_tool_definition.py tests\test_protocols.py tests\test_agent.py tests\test_run_ledger.py -q
# 149 passed, 2 skipped, 1 pytest cache warning, 44 subtests passed

python -m unittest tests.test_work_checkpoint_flow
# 15 passed

python -m pytest -q
# 1428 passed, 8 skipped, 1 pytest cache warning, 131 subtests passed

python -m ruff check codey tests
# All checks passed

python -m py_compile codey\tool_definition.py codey\protocols\json_codec.py codey\agent.py codey\run_ledger.py codey\__init__.py
# passed
```

## 0.2.26 Ledger Projections v1

Codey 0.2.26 adds a read-only projection layer over project Run Ledgers. It
turns append-only JSONL facts into bounded run summaries, then shadow-consumes
one production path: the final project task receipt. The legacy receipt remains
the source of truth unless the ledger projection is complete, not truncated, has
final changes, and matches the legacy `changed_count`, `checks_passed`, and
`restore_available` fields exactly.

Production changes:

- New `codey/run_ledger_projection.py` defines `RunLedgerProjection`,
  `ChangesSummary`, `VerifiedCommandSummary`, `ProviderFailureSummary`, and
  `ProviderSwitchSummary`.
- `project_run_ledger(records)` is a pure projection over `RunLedgerRecord`
  rows. It sorts by `seq`, ignores malformed/future/unknown events, and
  summarizes lifecycle state, provider switches/failures, model reply sizes,
  tool counts/errors, observed `file_changed` facts, verified commands, final
  changes, and truncation state.
- `changes_collected` now includes top-level `checks_passed`. Receipt projection
  uses only `changed_count`, `mode`, and `checks_passed`; it does not read the
  nested legacy `receipt` field.
- `TaskRunner` appends `run_finished`, loads the projection, and only then
  considers replacing the terminal event receipt with the projected receipt.
  Projection failure or mismatch falls back to the existing receipt path.
- UI/SSE shape, checkpoint, restore, `ExecutionEvidence`, Research ledger, API
  export, and headless behavior are unchanged.

Validation:

```text
python -m unittest tests.test_run_ledger_projection tests.test_run_ledger
# 15 passed

python -m unittest tests.test_server.TaskRunnerUiEventTests
# 3 passed

python -m unittest tests.test_server.SessionThreadingTests.test_run_task_emits_receipt_and_inline_changes tests.test_server.SessionThreadingTests.test_run_task_emits_provider_failure_diagnostic_on_error
# 2 passed, with existing Node url.parse deprecation warning

python -m unittest tests.test_work_checkpoint_flow
# 15 passed

python -m pytest tests\test_run_ledger_projection.py tests\test_run_ledger.py tests\test_server.py -q
# 143 passed, 1 skipped, 1 pytest cache warning

python -m pytest -q
# 1422 passed, 8 skipped, 1 pytest cache warning, 112 subtests passed

python -m ruff check codey tests
# All checks passed

python -m py_compile codey\run_ledger.py codey\run_ledger_projection.py codey\task_runner.py codey\server.py codey\__init__.py
# passed

git diff --check
# passed
```

## 0.2.25 Run Ledger v1

Codey 0.2.25 adds an observe-only project run ledger. It gives project coding
runs a bounded append-only JSONL fact stream without changing `agent.py`, the
web-model JSON tool protocol, UI/SSE events, receipts, checkpoints, restore, or
Research execution.

Production changes:

- New `codey/run_ledger.py` defines `RunLedgerStore` and `RunLedgerWriter`.
  Ledgers are stored below `state_home / "run_ledgers" / session_key /
  "<run_id>.jsonl"` and every row carries `schema_version`, `seq`, `ts`,
  `run_id`, and `session_id`.
- Ledger events include `run_started`, `provider_selected`, `model_reply`,
  `tool_started`, `tool_finished`, `file_changed`, `command_verified`,
  `changes_collected`, `provider_failure`, `provider_switched`,
  `ledger_truncated`, and `run_finished`.
- `model_reply` stores reply length and a bounded note, never the full model
  reply. Tool results store a bounded first line, never full source files,
  full shell output, browser DOM, or webpage text.
- The byte budget is derived from semantic constants:
  `MAX_LEDGER_EVENTS * LEDGER_BYTES_PER_EVENT_BUDGET` (currently about
  512 KiB). If the budget is exceeded, Codey writes one `ledger_truncated`
  event and disables further appends for that run. Other write failures fail
  open and do not break the task.
- `TaskRunner` opens the ledger for project/hybrid runs and projects existing
  facts into it. Hybrid runs may leave a lifecycle-only ledger when Research
  fails before the Project Writer phase; Research tool events are intentionally
  not part of 0.2.25.
- Terminal error paths append a bounded `provider_failure` event before
  `run_finished`, so ledger readers do not need to parse `task_done` to recover
  the provider diagnostic.
- `server.State()` without a durable `state_home` disables run ledgers. The
  production app still passes `DEFAULT_STATE_HOME`, while tests and embedded
  callers using bare `State()` do not write project-run ledgers into a real
  user `~/.codey` directory.

Validation:

```text
python -m unittest tests.test_run_ledger
# 9 passed

python -m unittest tests.test_work_checkpoint_flow
# 15 passed

python -m unittest tests.test_server.TaskRunnerUiEventTests
# 3 passed

python -m unittest tests.test_server.SessionThreadingTests.test_run_task_reads_empty_file_without_error_or_legacy_log_event tests.test_server.SessionThreadingTests.test_shell_request_includes_risk_explanation tests.test_server.SessionThreadingTests.test_run_task_emits_receipt_and_inline_changes tests.test_server.SessionThreadingTests.test_run_task_emits_provider_failure_diagnostic_on_error
# 4 passed

python -m pytest tests\test_run_ledger.py tests\test_server.py -q
# 137 passed, 1 skipped, 1 pytest cache warning

python -m pytest -q
# 1416 passed, 8 skipped, 1 pytest cache warning, 112 subtests passed

python -m ruff check codey tests
# All checks passed

python -m py_compile codey\run_ledger.py codey\task_runner.py codey\server.py codey\__init__.py
# passed

git diff --check
# passed
```

## 0.2.24 Coding Current Context

Codey 0.2.24 adds a thin coding read-model prompt after local tool results.
It surfaces current local facts to the web model -- files read this run,
existing files eligible for exact edit, changed files and verification status, and
the selected verification command -- without turning coding into a hard
controller.

Production changes:

- New `codey/coding_context.py` renders a bounded `Coding current local context`
  block. Empty state renders nothing. Path lists are normalized, deduplicated,
  capped at 8 entries, and the block explicitly says it is context, not a fixed
  tool order.
- `agent.run(..., coding_context_enabled=True)` now tracks `read_file_paths`
  separately from `known_file_paths`. Successful reads appear under "Files read
  this run"; successful reads and edits appear under "Existing files eligible
  for exact edit".
- Verification candidates are refreshed once after edits before the next tool
  prompt is sent. The context marks verification as fresh only when a
  successful command covers the selected candidate after the latest edit. When
  fresh, it says changed files are covered and no longer shows a runnable
  suggested-check JSON object, avoiding redundant re-verification loops.
- The context is appended only after normal local tool results. Protocol repair
  prompts, default verification reminders, and execution semantics remain
  unchanged; coding still keeps the historical multiple-top-level-JSON
  compatibility behavior.
- Qwen submission readiness now requires both retained composer text and an
  enabled send button. This prevents Codey from typing into a not-yet-hydrated
  Qwen page whose composer later clears before send. The older text-only
  retention helper was removed after the readiness helper replaced it.

Production-like live A/B evidence (2026-07-28, already-open web-provider tabs)
used `tests/manual/coding_current_context_ab.py`, which runs the real
`agent.run` loop on temporary projects with real local read/edit/run tools. The
baseline arm passes `coding_context_enabled=False`; the context arm uses the
production default `coding_context_enabled=True`.

Two-case live A/B summary:

```text
DeepSeek report: tests/manual/results/coding_current_context_ab-deepseek-20260728T122414.json
baseline: success 2/2, default_verification_reminders=2, turns=10, tool_calls=6, sent_chars=15729
context:  success 2/2, default_verification_reminders=0, turns=8,  tool_calls=6, sent_chars=17767
delta: reminders -2, turns -2, sent_chars +2038

MiMo report: tests/manual/results/coding_current_context_ab-mimo-20260728T122607.json
baseline: success 2/2, default_verification_reminders=1, turns=9, tool_calls=6, sent_chars=15555
context:  success 2/2, default_verification_reminders=0, turns=8, tool_calls=6, sent_chars=17767
delta: reminders -1, turns -1, sent_chars +2212

Qwen report: tests/manual/results/coding_current_context_ab-qwen-20260728T124810.json
baseline: success 2/2, default_verification_reminders=2, turns=10, tool_calls=6, sent_chars=15729
context:  success 2/2, default_verification_reminders=0, turns=8,  tool_calls=6, sent_chars=17767
delta: reminders -2, turns -2, sent_chars +2038
```

Qwen diagnostic note: the first Qwen A/B attempt failed before the model could
answer because the page was still hydrating. Codey filled the composer early;
when Qwen finished rendering, the input cleared and send failed. The provider
adapter fix above was applied before rerunning Qwen, and the rerun completed.

Validation:

```text
python -B tests\manual\coding_current_context_ab.py --self-test
# self-test ok

python -B tests\manual\coding_repair_prompt_ab.py --self-test
# self-test ok

python -m pytest tests\test_agent.py tests\test_coding_context.py tests\test_coding_current_context_ab.py tests\test_qwen.py -q
# 153 passed, 2 skipped, 1 pytest cache warning

python -m pytest tests\test_agent.py tests\test_protocols.py tests\test_coding_context.py tests\test_coding_current_context_ab.py tests\test_coding_repair_prompt_ab.py tests\test_qwen.py -q
# 196 passed, 2 skipped, 1 pytest cache warning, 25 subtests passed

python -m pytest -q
# 1407 passed, 8 skipped, 1 pytest cache warning, 112 subtests passed

python -m py_compile codey\agent.py codey\coding_context.py codey\qwen.py tests\manual\coding_current_context_ab.py
# passed

python -m ruff check codey tests
# All checks passed

git diff --check
# passed
```

## 0.2.23 Coding Protocol Typed Repairs

Codey 0.2.23 brings Research-style typed protocol repairs back into the coding
loop without changing coding execution semantics. The coding `JsonToolCodec`
now classifies protocol failures with `protocol_error_kind`, and `agent.run`
uses that kind to send narrower repair prompts to web models.

Production changes:

- `codey/protocols/json_codec.py` now emits typed error kinds for coding:
  `no_json`, `unknown_tool`, `invalid_args`, `direct_answer`,
  `native_tool_denial`, and `nested_tool_in_done`.
- `codey/agent.py` renders kind-specific repair prompts:
  unknown `write_file`/`write` -> `edit(content=...)`; invalid edit modes ->
  one edit mode only; invalid read offsets -> 1-based offsets; prose answers ->
  `done.summary`; website-native tool denial -> local-runner JSON; nested tool
  JSON inside `done.summary` -> call the tool directly. The repair prompt also
  tells models to preserve the previous intended path/content/
  old_string/new_string/command when those arguments are still valid.
- Review hardening: previous-intent repair examples are now generated only when
  they are still valid under the coding schema. Missing/empty `old_string`
  falls back to a legal generic edit example instead of teaching
  `old_string:""`, and `read_file` repair preserves valid numeric-string
  limits such as `"120"` by normalizing them to integers.
- Multi-JSON repair hardening: coding still accepts accidental multiple
  top-level JSON tool objects, but repair examples now select the offending
  object instead of blindly using the first object. Regression coverage locks
  `read_file first.py` followed by invalid `read_file second.py offset=0`, and
  `read_file first.py` followed by unknown `write_file second.txt`.
- Existing compatibility is preserved: accidental multiple top-level JSON tool
  objects still parse as multiple actions, and this release does not introduce
  a coding allowed-tools gate or verification candidate IDs.

Manual live A/B evidence (2026-07-28, already-open web-provider tabs, no local
tools executed by the probe) now measures the production repair renderer
directly. An earlier prototype run showed the same direction but was treated as
over-strong because it embedded ideal repaired shapes. After review, production
repair prompts generate previous-intent examples from the invalid JSON itself
instead of using unrelated placeholder values.

Production-prompt A/B:

```text
DeepSeek: baseline clean_repair=5/6 -> typed clean_repair=6/6
Qwen:     baseline clean_repair=4/6 -> typed clean_repair=6/6
          (one transient baseline send failure rerun for invalid_edit_mixed_modes)
MiMo:     baseline clean_repair=5/6 -> typed clean_repair=6/6
```

The production gains were exact parameter repairs. Typed repair preserved edit
newlines where the generic baseline often dropped them, and repaired
`read_file offset=0` into `offset=1` while keeping the original `limit=120`.

Manual-only Research probe archival:

- `tests/manual/concept_context_ab.py` records the negative/neutral Concept
  Context injection experiment and remains outside production Research prompts.

Validation:

```text
python -B tests\manual\coding_repair_prompt_ab.py --self-test
# self-test ok

python -B tests\manual\concept_context_ab.py --self-test
# self-test passed

python -m pytest tests\test_coding_repair_prompt_ab.py tests\test_protocols.py tests\test_agent.py -q
# 134 passed, 2 skipped, 1 pytest cache warning, 25 subtests passed

python -m pytest tests\test_server.py tests\test_consensus.py tests\test_review.py tests\test_agent_tools.py -q
# 173 passed, 2 skipped, 1 pytest cache warning

python -m pytest -q
# 1394 passed, 8 skipped, 1 pytest cache warning, 112 subtests passed

python -m py_compile codey\protocols\json_codec.py codey\agent.py tests\manual\coding_repair_prompt_ab.py tests\manual\concept_context_ab.py
# passed

python -m ruff check codey tests
# All checks passed

git diff --check
# passed
```

## 0.2.22 Concept Graph Seed

Codey 0.2.22 adds a concept layer on top of the knowledge vault: notes can
declare typed concept relations, relations are cached in a rebuildable SQLite
table, and a virtual Concept Graph read model feeds a single unified Research
drawer Graph. The persisted evidence graph is untouched, concepts never become
Markdown notes, co-tags never create edges, and missing-link candidates stay
text-only ("unproven; not facts"). By design this release does NOT inject
concept context into research prompts; the later manual A/B stayed outside
production after showing no reliable discovery gain.

Production changes:

- New `knowledge/concept_schema.py`: `normalize_concept` (lowercasing,
  whitespace folding, edge punctuation strip, URL / machine-tag / pure-year /
  single-Latin-char noise filtering, 48-char cap; single CJK chars kept),
  `clean_relations` (drops non-objects, empty/noisy endpoints, self-loops,
  duplicates; downgrades unknown kinds to `relates`; caps 8 per note; returns
  explicit warnings), `concept_tags`, and the 6-kind edge allowlist.
  `note.py` imports only this module -- no import cycle.
- `note.py` carries `relations` through `__post_init__` cleaning,
  front-matter output, and `from_markdown` tolerant parsing (Markdown stays
  authoritative). `index.py` caches a `concept_edges` table
  (`UNIQUE(note_id, src, dst, kind)` + src/dst indexes) with the same
  upsert/remove/clear lifecycle as tags, so `rebuild()` works unchanged;
  new read queries `tags_for` / `concept_edge_rows` / `tag_concept_rows`.
- Contract + tool split per review: `tool_contract.py` types
  `knowledge_write.relations` as a list of objects (a single object is
  normalized to a one-item list; non-object items are a typed `invalid_args`);
  `tools.py` then cleans leniently and appends
  `WARNING: relations: ...` to the tool result so models see what was
  dropped. Relation endpoints merge into note tags. The research prompt
  gains two discipline lines (concept-noun tags; declare only relations the
  sources actually state).
- New `knowledge/concepts.py` (`ConceptGraphBuilder`): declared relations
  become edges with support counts; declared endpoints weight >= 2.6 (label
  always visible), tag-only concepts <= 2.4; recent synthesis notes (max 12)
  attach via faint `tagged` edges; missing-link suggestions come only from
  declared adjacency (shared declared neighbor, no declared edge), capped at
  6, rendered only into concept-node excerpts, never persisted.
- New `GET /api/research/concept_graph` diagnostic endpoint (mirrors
  `/api/research/graph`) plus a unified Research drawer `Graph` tab. The
  production graph endpoint now composes concepts, the current synthesis/report,
  related notes, and source URLs into a bounded 3-depth read model; concept
  nodes render brighter with radius 5, `tagged` edges render as faint as `cites`.
- `runner._persist_synthesis` now aggregates the run's top-5 concept tags
  (Counter over normalized note tags) instead of machine tags only.

Review fixes (same release, after code review):

- Concept-node details list declared relations as grouped Outgoing/Incoming
  summaries with direction, kind, and supporting note titles; missing-link text
  is renamed to Open Questions and keeps the "Unproven; not facts" boundary.
  No edge-click UI added.
- `concept_edge_rows` / `tag_concept_rows` only read `status='active'` notes,
  so contradicted/superseded/stale notes stop feeding the concept layer. When a
  session is requested, both queries read that session first and then backfill
  globally, so older Research runs stay diagnosable after the vault grows past
  the scan cap.
- Synthesis tag aggregation also uses active-only note tags, so inactive note
  concepts cannot re-enter the Concept Graph through the active synthesis note.
- `node_limit` / `edge_limit` are hard: `_append_syntheses` spends only the
  leftover budget (regression: limits 8/8 stay <= 8/8 with syntheses present).
- `ConceptGraphBuilder` now sanitizes direct `node_limit` / `edge_limit`
  callers and selects declared relation endpoint pairs before filling leftover
  space with isolated tag concepts. Regression: 80 one-off relation pairs with
  `node_limit=64` return 32 visible relation edges instead of 64 isolated nodes
  and 0 edges. Edge selection prioritizes relations supported by notes from the
  current session, not shared concept names; regression: a 1-note current
  relation is not crowded out by old 3-note relations at `edge_limit=8`, even
  when both current and old relations share `war`. Regression: requested-session
  edge/tag rows survive a capped scan even when newer unrelated rows exceed the
  same cap, and the builder still renders the target `war -> helium` edge.
- Unified Graph trimming now protects the current evidence spine first
  (focus synthesis/report, related notes, and depth-3 source URLs), then spends
  remaining node/edge budget on global concepts. Regression: 140 unrelated
  global concept pairs no longer crowd out the current fact/source path.
- The default Research controller path now teaches the concept layer: two
  discipline lines in `controller_system_prompt` and tags + relations in the
  `knowledge_write` allowed JSON shape (also fixed a stray extra closing brace
  in that example).
- Empty unified Graph shows the builder's guidance warning in the quiet gray
  status line instead of "No graph yet".
- MiMo live-page root cause fixed: its rendered JSON code blocks expose both
  an `aria-hidden` overlay placeholder and the real content in `innerText`.
  The MiMo driver now strips hidden overlay layers before parsing, and the
  response copy path prefers the response-local raw copy over rendered overlay
  text when they differ. Live Edge verification on the open MiMo tab changed
  the first two replies from two `"tool"` occurrences to one each.
- Controller repair tightened for web models: when a model calls a currently
  forbidden tool such as first-turn `knowledge_write`, the controller returns
  `disallowed_tool` before contract argument repair, so Codey does not teach
  a tool shape that is not currently allowed. The allowed-actions block now
  says tools not listed are forbidden this turn.
- Source-node display polish: source URL nodes recover titles from the
  synthesis source ledger when available, and graph details render short
  Markdown excerpts via the existing zero-build `CodeyRender` renderer.
- UI consolidation: the drawer now exposes four tabs (`Evidence`, `Sources`,
  `Graph`, `Notes`) instead of separate evidence/concept graph tabs. The single
  Graph uses depth 1/2/3 for concepts + synthesis/report, related notes, and
  sources, while the internal concept endpoint remains available for tests and
  diagnostics.
- Docs synced: README (EN/zh) four-tab list with unified Graph wording;
  DESIGN.md documents the Research graph hover accent as the explicit
  `--ok-dot` exception; new production files are git-tracked.

Validation (deterministic fixtures, no live A/B by design):

```text
python -m pytest tests\test_mimo.py tests\test_research_controller.py tests\test_research.py tests\test_knowledge.py tests\test_ui.py tests\test_server.py tests\test_ui_browser_e2e.py tests\test_research_repair_prompt_ab.py -q
# 357 passed, 1 skipped, 1 pytest cache warning, 11 subtests passed (schema
# rules; note round-trip + index rebuild keep relations; declared relation
# direction/kind/note provenance; co-tags create no edges; missing suggestions
# land only in excerpts and are never persisted; non-active notes are excluded;
# synthesis tags only count active notes; node/edge limits hold with syntheses;
# relation endpoint pairs and current-session edges stay visible by note provenance; direct bad limit args sanitize;
# controller prompt + knowledge_write shape teach tags/relations and parse as
# JSON; disallowed tools take precedence over bad args; MiMo overlay duplicates
# are stripped; concept_graph endpoint and unified Graph depth/layer/budget UI
# assertions pass; repair-prompt A/B fixture is locked;
# real browser boot with the extended drawer/graph modules passes)

python -m pytest -q
# 1380 passed, 8 skipped, 1 pytest cache warning, 112 subtests passed

python -m ruff check codey tests
# All checks passed

python -m py_compile codey\knowledge\concepts.py codey\knowledge\unified_graph.py codey\knowledge\index.py codey\server.py
# ok

git diff --check
# passed
```

## 0.2.21 UI Asset Modularization

Codey 0.2.21 splits the single-file web UI into zero-build asset modules while
keeping visuals, DOM structure, `/api/*`, SSE reconciliation, the composer send
chain, and provider behavior unchanged. No npm, bundler, ESM, or framework was
introduced; assets are plain scripts and stylesheets loaded synchronously in a
fixed order.

Production changes:

- `server.py` replaced the hand-written asset dict with a safe resolver that
  only serves `/assets/*.js` and `/assets/*.css` resolved inside
  `web/assets/`; traversal, directories, and unknown extensions return 404.
  `index.html` is served through `_send_index()` with `__CODEY_VERSION__`
  replaced by `codey.__version__`, and every asset reference carries
  `?v=__CODEY_VERSION__` for cache busting.
- CSS tokens split: `:root` design tokens moved to `assets/tokens.css`, all
  remaining styles to `assets/app.css`; `index.html` has zero inline
  `<style>` lines.
- Research drawer split: drawer open/close/render, tabs, evidence/source/graph
  cards, and the research note cache moved to `assets/research_drawer.js`
  (`window.CodeyResearchDrawer`). Run recording/restore state stayed in index.
- Changes drawer split: drawer open/load/close/render plus diff chunk parsing
  and diff line rendering moved to `assets/changes_drawer.js`
  (`window.CodeyChangesDrawer`). `fetchChanges` / `restoreChanges` /
  receipt-side summary logic stayed in index.
- Render helpers split: pure helpers (`escapeHtml`, minimal markdown
  rendering, copy buttons, tool-line fold helpers) moved to
  `assets/render.js` (`window.CodeyRender`, no deps/init needed).
  `renderChat()` and `appendMessageNode()` stayed in index.
- Provider UI split: provider labels/availability, menu sync/refresh, status
  application, and local provider config popover handlers moved to
  `assets/provider_ui.js` (`window.CodeyProviderUI`). `currentProviderId` and
  `setActiveProvider` stayed in index and are injected via `init(deps)`.
- SSE ingestion/reconciliation, state/storage, session ops, the composer send
  chain, and boot remain in `index.html` as the thin core; extracted call
  sites go through thin same-name wrappers.
- New architecture ratchet `tests/test_ui_architecture.py`: inline `<style>`
  budget 0 lines; inline `<script>` budget 1950 lines (actual 1915, down from
  2698 before the split); one `window.Codey*` namespace per asset module;
  every referenced asset exists and carries the version placeholder; script
  load order is fixed (`render.js`, `research_graph.js`,
  `research_drawer.js`, `changes_drawer.js`, `provider_ui.js`, then the
  inline core).

Validation:

```text
python -m pytest tests\test_ui.py tests\test_ui_architecture.py tests\test_server.py -q
# 186 passed

python -m pytest tests\test_ui_browser_e2e.py -q
# 1 passed (real browser: modularized assets load, boot wiring works end to end)

python -m pytest -q
# 1349 passed, 112 subtests passed

python -m py_compile codey\server.py tests\test_ui.py tests\test_ui_architecture.py tests\test_server.py
# ok

python -m ruff check codey tests
# All checks passed

git diff --check
# clean
```

## 0.2.20 Research Controller v1

Codey 0.2.20 moves the manual thin-gate direction into production Research as a
thin, state-aware controller. The controller does not plan the research path and
does not replace the existing tool contract or report quality gate. It reads the
current ledger, appends a small allowed-actions block, and compiles stable-ID
actions into ordinary runtime tool arguments before execution.

Production changes:

- Added `codey.research.controller.ResearchController`.
- Production `ResearchRunner` enables the controller by default; tests and
  manual baselines can pass `controller_enabled=False`.
- First-turn Research prompt no longer lists all concrete tool JSON shapes.
  Instead, Codey appends the current allowed-actions block every turn.
- Search results, opened sources, and source_search hits get run-global stable
  IDs: `result_id`, `source_id`, and `hit_id`.
- Current controller protocol exposes distinct `open_result(result_id)`,
  `reopen_source(source_id, offset/pages)`, and `open_hit(hit_id)` actions
  before `JsonToolCodec` validates the final ordinary runtime arguments.
- `knowledge_write` may use `source_id` in `sources` and
  `evidence.source_url`; the controller rewrites those IDs to final opened URLs.
- `done` is allowed after saved evidence exists, with a narrow near-limit escape
  for no-citable-evidence reports. The existing report quality gate still
  decides whether the report is acceptable.

Validation focus:

- Initial controller state exposes only `knowledge_search`, `knowledge_read`,
  and `web_search`.
- `r1/r2/...` stay stable across multiple web searches even when later searches
  reorder or repeat URLs.
- `open_result` with `result_id` dispatches to the correct runtime open URL.
- `source_search` and `knowledge_write` can use `source_id`.
- `open_hit` opens the correct PDF page or HTML offset target.
- Controller blocks display the most recent result/source/hit IDs while keeping
  older ID mappings valid for parsing.
- Unknown `result_id`, `source_id`, and `hit_id` return typed `invalid_args`
  repairs instead of falling through to handwritten arguments.
- Stable-ID actions override conflicting handwritten URLs, so a model cannot
  combine `result_id/source_id/hit_id` with an unrelated `url` to bypass the
  controller.
- Early `done` is rejected as `disallowed_tool`; `done` becomes allowed after
  saved evidence or near the turn limit for insufficient-evidence reporting.
- `controller_enabled=False` preserves the full old Research prompt for manual
  baselines.

Validation:

```text
python -m pytest tests\test_research_controller.py tests\test_research.py -q
# 92 passed, 1 pytest cache warning

python -m pytest tests\test_deep_research_core_ab.py tests\test_research_controller.py tests\test_research.py -q
# 107 passed, 1 pytest cache warning

python -m pytest tests\test_research_controller.py tests\test_research.py tests\test_deep_research_core_ab.py tests\test_research_protocol_contract.py -q
# 125 passed, 1 pytest cache warning

python -B tests\manual\deep_research_core_ab.py --self-test
# self-test passed

python -m pytest tests\test_server.py tests\test_ui.py tests\test_research_controller.py tests\test_research.py tests\test_deep_research_core_ab.py tests\test_research_protocol_contract.py -q
# 292 passed, 1 skipped, 1 pytest cache warning

python -m pytest -q
# 1328 passed, 8 skipped, 1 pytest cache warning, 112 subtests passed

python -m ruff check codey tests
# All checks passed

python -m py_compile codey\research\controller.py codey\research\runner.py codey\research\protocols.py codey\research\tool_contract.py tests\manual\deep_research_core_ab.py
# passed
```

## 0.2.19 Research Browser Isolation and Thin-Gate Probe

Codey 0.2.19 fixes the deeper live Research stall seen with web providers:
Codey's browser-backed search/fetch pages were sharing the same Edge/CDP browser
as the provider chat tabs. Local AI did not stall because it did not depend on
that shared browser context. This release isolates Research browsing and records
the thin-gate A/B evidence for the next Research controller step.

Production changes:

- `BrowserSearchProvider` now defaults to an isolated Research browser profile
  at `~/.codey/research-edge-profile` and a separate preferred CDP port
  (`DEFAULT_PORT + 40`), instead of sharing `DEFAULT_PROFILE` / 9222 with
  provider chat tabs.
- Isolated CDP sessions use `_find_free_isolated_cdp_port()`, which ignores
  stale active/saved provider ports and cannot accidentally reuse the provider
  browser.
- Research search/fetch pages no longer call `bring_to_front()` by default.
- HTML fetch now retries short `Page.content()` races caused by pages that keep
  navigating or replacing content after `domcontentloaded`.
- Research UI event payloads, persisted UI state, and turn dividers preserve
  `RunEvent.note`, so `done` attempts show as `Turn N (done)` and protocol
  mistakes can show typed notes such as `Turn N (direct_answer)` instead of
  blank turns when Codey sends the model back for another action.
- `ResearchRunner` emits the report-quality failure message before asking the
  model to revise a failed `done`.

Manual A/B / live observations:

- The manual Deep Research A/B harness gained a `thin_gate` arm with
  state-aware allowed tools, stable `result_id` / `source_id` rewrites,
  evidence-backed citable-source separation, and atomic `send_start` traces.
- Live MiMo `long-official-doc/thin_gate` after the citable-source prompt fix:
  `done=True`, `quality_score=11`, `turns=8`,
  `protocol_repair_prompts=0`, and `id_rewrite_count=4`.
- The thin-gate result is evidence for the next narrow controller direction;
  production Research still keeps the existing open tool loop in 0.2.19.
- Live user smoke after the browser isolation fix confirmed the web-provider
  Research stall was resolved. The old symptom was that DeepSeek/StepFun/MiMo
  appeared to keep spinning after a Codey `web_search/open_url` result and only
  showed JSON after Codey was stopped.

Validation focus:

- Default `BrowserSearchProvider()` opens an isolated Research browser instead
  of reusing a provider/search tab in the shared CDP browser.
- Explicit `isolated=False` still preserves the old shared-browser contract for
  tests or diagnostics.
- Isolated free-port selection ignores stale remembered provider ports.
- `Page.content()` transient navigation errors are retried and then succeed.
- Failed final-report quality review now emits an info event.
- Backend UI event mapping, persisted UI state, and frontend turn handling keep
  turn notes such as `(done)` and `(direct_answer)`.
- Manual thin-gate prompt state separates opened-only sources from
  evidence-backed citable sources.

Validation:

```text
python -m pytest tests\test_research.py tests\test_browser.py tests\test_ui.py -q
# 166 passed, 1 pytest cache warning

python -m pytest tests\test_deep_research_core_ab.py tests\test_research_protocol_contract.py -q
# 33 passed, 1 pytest cache warning

python -B tests\manual\deep_research_core_ab.py --self-test
# self-test passed

python -m pytest -q
# 1311 passed, 8 skipped, 1 pytest cache warning, 112 subtests passed

python -m ruff check codey tests
# All checks passed

python -m py_compile codey\browser.py codey\research\browser_search.py codey\research\runner.py tests\test_browser.py tests\test_research.py tests\test_ui.py tests\manual\deep_research_core_ab.py tests\test_deep_research_core_ab.py
# passed

git diff --check
# passed with Git CRLF notice for tests/test_browser.py
```

## 0.2.18 Research Tool Contract and Typed Repairs

Codey 0.2.18 makes the Research JSON fallback behave more like a local tool
contract. It does not add the full Research controller/state machine yet; the
goal is narrower: reject malformed Research tool calls before execution, give
the model a precise repair message, and avoid two live runtime issues seen while
probing Qwen and MiMo.

Production changes:

- Added `codey/research/tool_contract.py` with typed contracts for
  `web_search`, `open_url`, `source_search`, `knowledge_search`,
  `knowledge_read`, `knowledge_write`, `knowledge_link`, and `done`.
- Added `codey/research/protocol_diagnostics.py` for conservative no-JSON
  diagnostics: `no_json`, `direct_answer`, and `native_search_leak`.
- Added `ToolPlan.protocol_error_kind` as a backward-compatible optional field.
- `JsonToolCodec` now validates and normalizes Research arguments before tool
  dispatch. Optional defaults are used only when a field is missing; malformed
  numbers are `invalid_args`, and unknown extra args are dropped.
- `ResearchRunner` now sends typed repair prompts for `unknown_tool`,
  `too_many_tools`, `invalid_args`, `direct_answer`, `native_search_leak`, and
  `no_json` instead of using one generic repair prompt for every protocol error.
- `knowledge_write type="synthesis"` is now rejected by the Research contract.
  Final reports must use `done`; Codey persists the synthesis after report
  quality review passes.
- Browser-backed Research search/fetch now uses a dedicated lazy
  `codey-research-browser` worker instead of reentering the provider browser
  worker. This fixes the live error:
  `It looks like you are using Playwright Sync API inside the asyncio loop`.
- MiMo now waits for its response footer/copy action to become stable before
  returning a response to the upper loop, matching the StepFun-style pacing
  fix for pages whose answer text stabilizes before the action/composer area is
  ready.

Validation focus:

- `source_search` missing `query` returns `invalid_args` with a source_search
  example.
- Legacy tool and argument aliases such as `open`/`fetch`, `queries`, and
  `done.summary` are rejected instead of normalized.
- `open_url offset="12"` is accepted, while `offset="abc"` is rejected.
- `knowledge_write.evidence` accepts a single object by normalizing it to a
  one-item list, but rejects non-object evidence entries such as `["bad"]`.
- Unknown tools, too many JSON tool calls, direct prose reports, and suspected
  native-search leaks get distinct protocol error kinds.
- `knowledge_write type="synthesis"` gets a repair that points to `done`.
- Research browser search called from the provider browser worker uses the
  dedicated Research worker instead of the provider worker.
- MiMo waits for a stable response footer before returning, and a live
  continuous long-message submit probe completed two sends without timeout.

Live provider observations:

- Qwen `long-official-doc/source_search`, 10-turn smoke:
  `used_source_search=True`, opened the target offset, saved exact evidence,
  `protocol_repair_prompts=0`, but `done=False` because Qwen spent the turn
  budget writing intermediate notes.
- MiMo before footer wait: the typed contract caught a first-turn
  `too_many_tools` flood and repaired it, but a later send hit
  `Xiaomi MiMo Chat send failed (transient)`.
- MiMo after footer wait and final-report clarification:
  `long-official-doc/source_search` completed in 9 turns with `done=True`,
  `quality_score=10`, `used_source_search=True`, target offset opened, exact
  evidence saved, and report quality passed.

Validation:

```text
python -m pytest tests\test_research_protocol_contract.py tests\test_research.py tests\test_mimo.py tests\test_ui.py tests\test_protocols.py tests\test_deep_research_core_ab.py -q
# 229 passed, 1 pytest cache warning, 36 subtests passed

python -m pytest -q
# 1306 passed, 8 skipped, 1 pytest cache warning, 112 subtests passed

python -B tests\manual\deep_research_core_ab.py --self-test
# self-test passed

python -m ruff check codey tests
# All checks passed

python -m py_compile codey\mimo.py codey\browser_worker.py codey\research\browser_search.py codey\models.py codey\research\tool_contract.py codey\research\protocol_diagnostics.py codey\research\protocols.py codey\research\runner.py tests\test_mimo.py tests\test_research.py tests\test_research_protocol_contract.py tests\manual\deep_research_core_ab.py
# passed
```

## 0.2.17 Source Search Production and Research Tool Boundary

Codey 0.2.17 promotes the deterministic `source_search` locator from the manual
Deep Research A/B harness into production Research, and tightens the Research
JSON-tool boundary while keeping `deep_core` manual-only.

Production changes:

- Added `codey/research/source_search.py` for deterministic token locator search
  over already-opened source text or PDF page text.
- Added read-only source accessors and source-search audit records to the
  per-run `ResearchLedger`.
- Added `ResearchTools.source_search()` and `ResearchRunner` dispatch.
- Added `source_search` to the default Research JSON protocol, with
  `JsonToolCodec(include_source_search=False)` for manual baseline isolation.
- Added a hard Research boundary that tells providers not to use the chat
  website's built-in search/browsing/plugins or outside knowledge.
- Added one-tool-per-turn Research discipline in the system prompt, protocol
  repair prompt, and tool-result follow-up.
- Tightened the production Research JSON parser so multiple tool calls in one
  reply are rejected and repaired instead of being executed as a batch.
- PDF source_search can bounded-scan an already-opened PDF URL for locators, but
  it does not update `pages_read` or evidence.
- HTML source_search returns offsets and previews. It remains a soft locator
  discipline rather than a hard returned-window provenance gate.
- Added a manual-only `--single-tool-boundary` probe switch to
  `deep_research_core_ab.py` for provider diagnosis.

Validation focus:

- HTML `open_url` followed by `source_search` finds a late offset without a
  second fetch, creates no evidence by itself, and records audit coverage.
- HTML evidence from opened-source ledger text remains accepted without adding
  an HTML range hard gate.
- PDF `source_search` can locate p.9 after the PDF URL was opened once, but
  `knowledge_write evidence.page=9` still fails until `open_url pages="9"`
  reads that page.
- Manual A/B baseline prompt remains source_search-free while source/deep arms
  keep source_search available.
- Research parser rejects multiple JSON tool calls in a single provider reply.
- Live MiMo follow-up:
  - Earlier no/single-tool experiments were mixed: without the extra boundary
    MiMo emitted multiple search calls; one single-tool run was clean but hit
    the turn cap before evidence.
  - Final fresh-tab long run with `--single-tool-boundary` completed
    `long-official-doc/source_search` in 10 turns with `quality_score=11`,
    `done=True`, `protocol_repair_prompts=0`, opened the source_search target
    offset, saved exact evidence, and passed report quality.

Validation:

```text
python -m pytest tests\test_research.py tests\test_deep_research_core_ab.py tests\test_ui.py -q
# 129 passed, 1 pytest cache warning

python -m pytest -q
# 1279 passed, 8 skipped, 1 pytest cache warning, 112 subtests passed

python -B tests\manual\deep_research_core_ab.py --self-test
# self-test passed

python -m ruff check codey tests
# All checks passed

python -m py_compile codey\__init__.py codey\research\source_search.py codey\research\ledger.py codey\research\tools.py codey\research\runner.py codey\research\protocols.py tests\test_research.py tests\test_deep_research_core_ab.py tests\manual\deep_research_core_ab.py tests\test_ui.py
# passed

git diff --check
# passed
```

## 0.2.15 Source Search Research Hygiene

Codey 0.2.15 records the cross-provider Deep Research A/B result and fixes the
small production issues found while probing Qwen.

Production changes:

- Qwen now fills the composer until the text remains stable for a short settle
  window. If the page finishes hydration and clears the draft, Codey refills a
  bounded number of times before submitting.
- Research dispatch now requires canonical `query` args; model-emitted
  `queries` aliases fail through the normal protocol/argument error path.
- The manual `source_search` A/B arm keeps its own historical diagnostics, but
  production Research no longer treats `queries` as a success path.
- Manual A/B harness `fresh_tab` and `keep_open_on_error` controls now default
  to `False`, preserving older scripted calls.
- Report quality accepts URL-first numbered source entries such as
  `1. https://standards.example.org/widget-storage - Widget Storage standard`
  while keeping the existing opened-source and evidence-snippet checks.

Live/manual A/B findings:

- Qwen `long-official-doc` baseline at 12 turns: `score=4`, `done=False`,
  `max_turns=True`; it cited unopened public URLs and did not save the target
  fixture evidence.
- Qwen `long-official-doc` source_search at 12 turns: `score=10`,
  `done=True`; it used `source_search`, opened the target offset, saved
  evidence, and passed the quality gate.
- Earlier Qwen 10-turn source_search reached the evidence path but needed more
  turns to finish; this informed the 12-turn follow-up instead of changing
  production defaults.
- Local Gemma4-12B (`http://localhost:5001/v1`, model `Gemma4-12B`) passed the
  JSON smoke. On fixtures, source_search reduced turns for the PDF case
  (`5` vs baseline `6`) and stayed competitive on long-document cases. Local
  output still needs protocol tolerance because it can fence JSON, and heavier
  `deep_core` prompts can trigger repair loops or public-URL drift.
- Combined with the existing DeepSeek and StepFun reports, the A/B direction is
  now clear: deterministic `source_search` is useful across web and local
  providers. The heavier `deep_core` plan/coverage prompt is still not ready for
  default production use.

Validation:

```text
python -m pytest tests\test_deep_research_core_ab.py tests\test_qwen.py tests\test_research.py tests\test_ui.py -q
# 169 passed, 1 pytest cache warning

python -m pytest -q
# 1270 passed, 8 skipped, 1 pytest cache warning, 112 subtests passed

python -B tests\manual\deep_research_core_ab.py --self-test
# self-test passed

python -m ruff check codey tests
# All checks passed

python -m py_compile codey\__init__.py codey\qwen.py codey\research\runner.py codey\research\report_quality.py tests\test_qwen.py tests\test_research.py tests\test_ui.py tests\test_deep_research_core_ab.py tests\manual\deep_research_core_ab.py
# passed

git diff --check
# passed
```

## 0.2.14 StepFun Submit Stability

Codey 0.2.14 tightens the StepFun provider adapter after live probes showed
that the page could expose reply text before its footer actions had finished
rendering. A follow-up prompt sent during that gap could remain in the composer
without being submitted.

Production changes:

- Updated StepFun's provider profile to prefer the current
  `button:has(svg.custom-icon-send-outline)` send control.
- Added a StepFun-specific response action selector for the reload footer button
  and waits for that footer to stabilize before returning a completed response
  to the upper agent loop.
- Made StepFun submission confirmation stricter: newline insertion or changed
  textarea contents no longer count as a successful submit.
- Added a force-click retry for the profiled send button, then fail fast with
  `SubmissionUncertain` if the submit cannot be confirmed.
- Kept the fix provider-local. No UI, router, agent prompt, review, or Research
  production behavior changed.

Manual diagnostics:

- `tests/manual/provider_submit_probe.py` adds a small live submit/idle smoke
  for any web provider.
- `tests/manual/stepfun_submit_probe.py` keeps a StepFun-focused version for
  inspecting composer/send/response state.
- `tests/manual/deep_research_core_ab.py` can now run in a fresh provider tab
  and optionally keep an error tab open for diagnosis.

Validation:

```text
python -m pytest tests\test_stepfun.py tests\test_provider_profiles.py tests\test_ui.py -q
# 64 passed, 1 pytest cache warning

python -B tests\manual\deep_research_core_ab.py --self-test
# self-test passed

python -m ruff check codey tests
# All checks passed

python -m py_compile ...
# passed

git diff --check
# passed
```

## 0.2.13 Provider Fit Update

Codey 0.2.13 adds StepFun while keeping MiMo. The change documents provider fit
instead of adding automatic routing: users can still choose a provider manually,
and the core agent/review/tool path remains unchanged.

Production changes:

- Added the `stepfun` provider id, `codey/stepfun.py`, and
  `StepFunWebProvider` for `https://chat.stepfun.com/chats/`.
- Restored MiMo as a supported provider alongside StepFun.
- Updated browser warmup, provider registry/profile entries, repair policy,
  UI labels, README/CHANGELOG provider lists, and manual A/B examples.
- Kept the change provider-local: no role router, no new UI mode, no broad
  agent prompt change, and no provider-independent review/tool/runtime rewrite.

Live probe results:

- StepFun Research fixture probe completed with exactly one JSON tool call per
  turn and no prose outside JSON.
- MiMo remains useful for coding/editing based on historical live coding
  results, but the strict Research fixture is not a good fit because it often
  emits multiple or malformed JSON tool calls.
- MiniMax was not selected: its Agent page ignored the local JSON-tool protocol
  on the first probe and used its own web/agent behavior.
- StepFun coding smoke `edit` passed after removing the StepFun-specific prompt
  hint. The first three replies used non-JSON `<tool_call>` markup, but Codey's
  existing protocol nudge recovered the run; it then edited the file and passed
  `python -m unittest`.
- StepFun coding smoke `create` failed because the model wrote invalid Python
  indentation / `if name == 'main':` and then no-op repairs. This is a model
  coding-quality limitation, not an adapter DOM compatibility failure.

Validation:

```text
python -m pytest tests\test_mimo.py tests\test_stepfun.py tests\test_providers.py tests\test_browser.py tests\test_provider_profiles.py tests\test_ui.py tests\test_server.py tests\test_adapter_self_repair.py tests\test_provider_supervisor.py tests\test_provider_flow_fault_injection.py tests\test_review_coordinator.py tests\test_work_checkpoint_flow.py tests\test_live_smoke.py tests\test_bootstrap_smoke.py tests\test_tool_runtime.py -q
# 473 passed, 20 subtests passed

python -m pytest -q
# 1257 passed, 8 skipped, 112 subtests passed

python -m ruff check codey tests
# All checks passed

python -m py_compile ...
# passed

git diff --check
# passed

python -B tools\live_smoke.py --provider stepfun --case edit --max-turns 8 --json
# ok=true, 8 turns after protocol nudges

python -B tools\live_smoke.py --provider stepfun --case create --max-turns 10 --json
# ok=false, Python syntax repair failed
```

## 0.2.12 Research A/B and Provider Parsing Hygiene

Codey 0.2.12 keeps Deep Research Core behind the manual A/B harness, but makes
the probe cheaper to run and easier to debug while fixing two production
Research/provider parsing edge cases seen during live provider tests.

Production changes:

- DeepSeek can now return stable malformed JSON-tool-shaped replies so the
  Research protocol repair loop can correct them instead of waiting for the
  provider send timeout.
- Research report quality accepts Chinese-adjacent citations such as
  `结论[1]`.
- Research provenance parsing treats Chinese brackets, backticks, and quotes as
  URL boundaries, avoiding false unopened-source failures for ordinary Markdown
  or Chinese prose.
- CDP attach timeout was temporarily raised for loaded browser sessions in this
  historical release; the current 0.4.3 path has since restored the default
  browser attach/port waits to 20s for faster failure feedback.

Manual Research A/B changes:

- `tests/manual/deep_research_core_ab.py` defaults to a low-send `cheap`
  profile: two high-signal fixture cases, all arms, and a 10-turn cap.
- The probe prompt tells web models to use only local JSON tools and not the
  chat site's own web search or outside knowledge.
- Live runs atomically write a `.trace.json` beside the output after each reply
  and include send/reply counts, done attempts, repair prompt counts, opened
  sources, evidence items, raw reply previews, and last `done` quality review.

Live provider note:

- Xiaomi MiMo was smoke-tested on the `long-official-doc` fixture and is not a
  good primary model for the current JSON-tool Deep Research loop. The web page
  adapter was usable, but MiMo repeatedly emitted multiple JSON tool calls in a
  single reply, `json`-prefixed duplicated objects, malformed query strings with
  unescaped double quotes, and context-losing repair replies. It did not
  reliably reach `done` within the 10-turn cheap profile. This is a model
  protocol-following limitation for this research workflow, not evidence that
  MiMo is broken for ordinary chat or simpler tasks. Prefer DeepSeek, Qwen, or
  GLM when evaluating Deep Research source-search behavior.
- DeepSeek was re-run on the missing `long-official-doc` / `deep_core` arm after
  an earlier transient send failure. The clean retry completed in 8 turns,
  used `source_search`, opened the hidden target offset, reported the 72-hour
  threshold, saved an exact evidence snippet, and passed the report quality
  gate with `quality_score=10`. This validates the direction for
  `source_search` plus compact plan/coverage on long-source research: baseline
  completed but missed the hidden fact, source_search found evidence but did
  not finish within 10 turns, and deep_core both found the fact and finished.

Validation:

```text
python -m pytest tests\test_research.py tests\test_deepseek.py tests\test_deep_research_core_ab.py -q
# 87 passed

python -B tests\manual\deep_research_core_ab.py --self-test
# self-test passed

python -m ruff check codey tests
# All checks passed

python -m py_compile codey\__init__.py codey\browser.py codey\deepseek.py codey\research\provenance.py codey\research\report_quality.py tests\manual\deep_research_core_ab.py
# passed

git diff --check
# passed
```

## 0.2.11 Provider Readiness Self-Repair

Codey 0.2.11 teaches the provider self-repair path a narrow readiness drift
case without changing the UI or bypassing the existing adapter canary.

Production changes:

- Added `readiness_stale` as a typed provider failure so adapters can report
  cases where safe DOM facts indicate the page may be usable even though an
  adapter readiness signal is stale.
- Added bounded `ProviderFailure` facts with an explicit readiness allowlist:
  `composer_visible`, `send_visible`, `model_selector_text_present`,
  `response_count`, `question_count`, and `waited_for`.
- Treated `readiness_stale` as structural for provider circuit and self-repair
  enqueue logic while preserving the existing requirement that self-repair only
  queues after the circuit opens.
- Forwarded failure kind, stage, and sanitized facts from `SelfRepairJob`
  through the subprocess worker into the adapter repair prompt.
- Kept adapter override installation, UI, provider canary, and user project
  access boundaries unchanged.

Validation:

```text
python -m unittest tests.test_provider_diagnostics tests.test_provider_supervisor tests.test_adapter_self_repair
# 68 tests OK

python -m pytest tests\test_provider_diagnostics.py tests\test_provider_supervisor.py tests\test_adapter_self_repair.py tests\test_server.py tests\test_ui.py tests\test_qwen.py -q
# 286 passed

python -m pytest -q
# 1246 passed, 111 subtests passed

python -m ruff check codey tests
# All checks passed

python -m py_compile codey\__init__.py codey\provider_diagnostics.py codey\provider_supervisor.py codey\self_repair.py codey\self_repair_worker.py codey\adapter_repair.py codey\provider_worker.py codey\server.py codey\qwen.py
# passed

git diff --check
# passed
```

## 0.2.10 Research Quality and Provider JSON Hygiene

Codey 0.2.10 tightens the basic Research and chat/provider runtime path without
turning the experimental Deep Research Core probe on by default.

Production changes:

- Added shared JSON-tool reply helpers in `codey/json_tool_reply.py` and reused
  them from DeepSeek and Qwen.
- Updated Qwen readiness/completion handling for the current page shape:
  composer readiness no longer depends on the stale `/api/v2/models/` bootstrap
  request, stable JSON tool replies can complete quickly, and DOM JSON can win
  over stale copied text.
- Relaxed Research report source parsing for common source-line formats while
  keeping opened-source/evidence requirements strict for cited claims.
- Split Research provenance review by section, so limitations/search-coverage
  prose can mention unopened search-result domains without pretending they were
  cited sources.
- Added an explicit no-citable-source report path for searched-but-unverified
  Research runs.
- Made Research quality repair prompts specific and removed the old
  `CodeyResearch` name from the protocol prompt.
- Plain chat replies now carry `run_id`; chat `task_done` events carry the
  answer summary; the frontend de-duplicates answer events and can restore a
  chat answer from either `reply` or `task_done`.
- Added the manual `deep_research_core_ab.py` harness for source-search /
  plan / coverage experiments without changing default Research behavior.

Validation:

```text
python -m pytest tests\test_research.py tests\test_qwen.py tests\test_deepseek.py tests\test_deep_research_core_ab.py -q
# 127 passed

python -B tests\manual\deep_research_core_ab.py --self-test
# self-test passed

python -m pytest tests\test_research.py tests\test_server.py tests\test_ui.py tests\test_qwen.py tests\test_deepseek.py tests\test_mimo.py tests\test_glm.py tests\test_deep_research_core_ab.py -q
# 370 passed, 11 subtests passed

python -m ruff check codey tests
# All checks passed

python -m py_compile codey\research\protocols.py codey\research\provenance.py codey\research\report_quality.py codey\research\runner.py codey\qwen.py codey\deepseek.py codey\json_tool_reply.py
# passed

git diff --check
# passed

python -m pytest -q
# 1239 passed, 111 subtests passed
```

Live smoke:

```text
DeepSeek JSON done smoke: passed
MiMo JSON done smoke: passed
GLM JSON done smoke: passed
Qwen full smoke was skipped after the account hit its usage limit.
User-confirmed Codey UI "你好" chat smoke: web model reply appeared in the Codey chat UI.
```

## 0.2.9 Runtime Responsiveness Hygiene

Codey 0.2.9 improves runtime responsiveness without changing API payloads or
frontend behavior. The changes target slow restore/review tails and slow SSE
clients.

Production changes:

- `State.emit()` now drops the oldest queued SSE event when a subscriber queue
  is full, then enqueues the newest event.
- `State.restore_research_changes()` now schedules a coalesced background
  knowledge index rebuild instead of calling `knowledge_store.rebuild()` on the
  HTTP restore path.
- `ReviewCoordinator.run_cycle()` now reuses the project map refreshed for the
  current review cycle when a rejected review triggers Writer repair.
- Did not add incremental indexing, a workspace watcher, a background task
  framework, frontend changes, or API payload changes.

Validation:

```text
python -m unittest tests.test_server.RunSnapshotTests.test_emit_full_subscriber_queue_drops_oldest_and_keeps_latest tests.test_server.RunSnapshotTests.test_restore_research_changes_schedules_rebuild_in_background tests.test_server.RunSnapshotTests.test_restore_research_changes_coalesces_background_rebuilds tests.test_server.RunSnapshotTests.test_restore_research_changes_rebuild_error_does_not_change_restore_result: 4 tests OK
python -m unittest tests.test_review_coordinator: 12 tests OK
python -m py_compile codey\server.py codey\review_coordinator.py: passed
python -m unittest tests.test_server.RunSnapshotTests tests.test_review_coordinator: 26 tests OK
python -m unittest tests.test_server tests.test_ui tests.test_review_coordinator: 183 tests OK
python -m unittest discover: 1193 tests OK
python -m py_compile codey\__init__.py codey\server.py codey\review_coordinator.py: passed
python -m ruff check codey tests: All checks passed
git diff --check: passed
```

## 0.2.8 Research TaskRunner Hygiene

Codey 0.2.8 keeps TaskRunner behavior unchanged while making the core run
orchestrator easier to maintain. `TaskRunner.run()` now owns lifecycle and thin
mode dispatch; private helpers own chat, research, hybrid, and project path
execution.

Production changes:

- Added private `_RunFrame`, `_RunWork`, `_RunHooks`, and `_ModeOutcome`
  implementation details inside `task_runner.py`.
- Added `_run_chat_mode()`, `_run_research_mode()`, `_run_hybrid_mode()`, and
  `_run_project_mode()` on `TaskRunner`.
- Kept run reservation/start, provider preflight, cancellation/error cleanup,
  and terminal `finish_run()` in `TaskRunner.run()`.
- Did not add a strategy system, new task mode, router, module split, payload
  change, or provider/review/memory policy change.

Validation:

```text
python -m unittest tests.test_server.SessionThreadingTests: 65 tests OK
python -m unittest tests.test_server tests.test_research tests.test_project_task_context: 183 tests OK
python -m unittest tests.test_server tests.test_ui: 167 tests OK
python -m unittest discover: 1189 tests OK
python -m py_compile codey\task_runner.py: passed
python -m py_compile codey\__init__.py codey\task_runner.py codey\server.py: passed
python -m ruff check codey tests: All checks passed
git diff --check: passed
```

## 0.2.7 Research Server Hygiene

Codey 0.2.7 keeps Research server behavior unchanged while making the HTTP
handler easier to extend. The Research Graph, Research note, Research restore,
and run-submit response paths now use small `server.py` helpers that return
`(status, payload)`; `do_GET` and `do_POST` remain simple dispatch plus
`_send_json()`.

Production changes:

- Added `_research_unconfigured_response()`, `_research_graph_response()`,
  `_research_note_response()`, `_research_restore_response()`, and
  `_run_submit_response()` inside `server.py`.
- Kept `State.restore_research_changes()` and `_submit_task()` unchanged.
- Kept all API payloads and status codes unchanged.
- Did not add a router, a new `server/research.py` module, a schema change, or
  any frontend behavior change.

Validation:

```text
python -m unittest tests.test_server: 118 tests OK
python -m unittest tests.test_knowledge tests.test_server tests.test_ui: 175 tests OK
python -m unittest tests.test_ui_browser_e2e.UiBrowserE2ETests.test_complete_project_flow_in_real_edge: 1 test OK
python -m unittest discover: 1187 tests OK
python -m py_compile codey\__init__.py codey\server.py: passed
python -m ruff check codey tests: All checks passed
git diff --check: passed
git diff --cached --check: passed
```

## 0.2.6 Frontend Research Graph Split

Codey 0.2.6 moves the Research drawer Graph implementation into a dedicated
whitelisted browser asset while keeping the Research product surface unchanged.
The split is deliberately narrow: no CSS split, no ES modules, no bundler, and
no frontend framework migration.

Production changes:

- Added `codey/web/assets/research_graph.js` with the Graph toolbar, graph
  fetch, loading/error/empty state, canvas force layout, hover/click/dblclick
  behavior, and detail panel rendering.
- Kept `index.html` responsible for Research drawer tabs, session/run state,
  note cache, and the thin `renderResearchGraph()` / `disposeResearchGraph()`
  wrapper callbacks.
- Added a narrow `WEB_ASSETS` whitelist route for `/assets/research_graph.js`.
  Codey still does not serve `codey/web` as a general static directory.
- Added the `?v=0.2.6` script cachebuster so webview/browser sessions load the
  split graph module for this release.

Validation:

```text
bundled node --check codey\web\assets\research_graph.js: passed
python -m unittest tests.test_ui.ProviderSelectorUiTests.test_research_context_is_explicit_and_user_facing tests.test_server.WebAssetTests: 3 tests OK
python -m unittest tests.test_ui tests.test_server: 159 tests OK
python -m unittest tests.test_knowledge tests.test_server tests.test_ui: 169 tests OK
python -m unittest discover: 1181 tests OK
python -m py_compile codey\server.py: passed
python -m ruff check codey tests: All checks passed
git diff --check: passed
git diff --cached --check: passed
```

## 0.2.5 Research Graph

Codey 0.2.5 adds an on-demand Local Graph inside the Research drawer. The graph
is a read model over Markdown notes and the rebuildable SQLite index; it does
not add a graph database, a new Research mode, or localStorage graph artifacts.

Production changes:

- Added `codey/knowledge/graph.py` with bounded `ResearchGraphArtifact`,
  `GraphNode`, `GraphEdge`, and `KnowledgeGraphBuilder`.
- Added `KnowledgeIndex.notes_by_ids()`, `links_touching()`, and
  `sources_for()` for graph read-model queries without changing the schema.
- Added `GET /api/research/graph` with focus/depth/limit/source/counterpoint
  query support. Unknown focus/session returns an empty graph packet instead of
  a 500.
- Added a `Graph` tab to the Research drawer. It fetches graph data only when
  the tab is opened, renders a lightweight canvas force graph, widens the
  drawer only for Graph, and supports Depth 1/2, Reset, hover/click detail,
  URL source opening, and note handoff to the Notes tab.
- URL sources are virtual `source_url` nodes connected by virtual `cites` edges.
  `cites` remains display-only and is not added to persistent `LINK_KINDS`.
- Virtual counterpoint nodes are created only from the current run payload when
  no real `contradicts` link exists; the builder does not parse synthesis
  Markdown sections.
- The graph keeps Codey's monochrome design: grey/white nodes and links,
  dashed `contradicts`, and `--ok-dot` only as a hover accent for the hovered
  node and its connected edges.

Validation:

```text
python -m py_compile .\codey\knowledge\index.py .\codey\knowledge\graph.py .\codey\server.py: passed
python -m unittest tests.test_knowledge tests.test_research tests.test_server tests.test_ui: 215 tests OK
python -m unittest discover: 1178 tests OK
python -m unittest tests.test_knowledge tests.test_server.ResearchGraphApiTests tests.test_ui.ProviderSelectorUiTests.test_research_context_is_explicit_and_user_facing: 12 tests OK
```

Browser smoke:

```text
Temporary fixture server with synthesis/fact/counterpoint/implementation/verification notes.
Research drawer -> Graph tab: canvas rendered, drawer graph-open=true, loading overlay hidden.
Canvas screenshot sampling: 1280x900, non-dark pixels present, hover accent pixels present.
Screenshots captured for narrow and desktop graph states.
```

## 0.2.4 Research PDF Intake

Codey 0.2.4 makes PDF a first-class Research source intake path under
`open_url`, without adding a new user mode or tool surface.

Production changes:

- Added `SourceDocument` / `SourcePage` as the canonical opened-source model for
  HTML and PDF intake.
- Added bounded `pypdf` extraction for text PDFs: default page range, max bytes,
  max pages per open, max extracted text, scanned/empty PDF skip, and extraction
  failure skip.
- `BrowserSearchProvider.fetch()` now streams PDF bytes with a hard cap and
  returns PDF metadata without running PDF extraction on the browser-worker
  thread. Known `.pdf` URLs bypass browser page navigation entirely.
- PDF redirects are handled manually with redirect disabled in urllib. Each
  target URL is checked before the next request, so a public URL cannot redirect
  into localhost or private-network targets.
- Non-`.pdf` URLs that reveal `application/pdf` after browser navigation return
  a lightweight `pdf_download` sentinel from the browser worker; the actual
  streaming download then runs back on the Research thread.
- `ResearchTools.open_url()` now accepts `pages`, converts PDF bytes into a
  `SourceDocument`, records PDF opened-source metadata, and returns page-marked
  text such as `[page 4]`.
- Evidence Ledger records PDF `content_kind`, MIME type, total pages, pages
  read, truncation state, and snippet locators. Evidence can carry
  `evidence.page`, infer the page from an exact snippet, and replace bad PDF
  excerpts with exact opened-page text.
- `report_quality.py` accepts `[1 p.4]`, `[1 pp.4-5]`, and `[1 page 4]`
  citations, and rejects page citations whose pages were not read or lack
  snippet-backed evidence.
- Research drawer source/evidence cards show PDF page locators and PDF
  page/truncation metadata. Research advisor packs and Project Briefs carry the
  same page-aware evidence.
- Added runtime dependency `pypdf>=6.0,<7`.

Validation:

```text
python -m unittest tests.test_research -v: 47 tests OK
python -m unittest tests.test_server tests.test_ui tests.test_project_task_context tests.test_knowledge: 175 tests OK
python -m unittest discover: 1171 tests OK
python -m py_compile codey\research\source_document.py codey\research\pdf_extract.py codey\research\browser_search.py codey\research\tools.py codey\research\ledger.py codey\research\report_quality.py codey\research\runner.py codey\research\advisors.py: passed
python -m ruff check codey tests: passed
git diff --check: passed
```

Real PDF smoke:

```text
https://www.caict.ac.cn/kxyj/qwfb/bps/202408/P020240830315324580655.pdf
open_url pages=1-2: read page_count=83, pages_read=[1, 2], content_kind=pdf
extracted title text includes 中国数字经济发展研究报告
```

## 0.2.3 Research Provenance and Project Memory Hygiene

Codey 0.2.3 tightens the Research quality gate added in 0.2.2 and locks the
research-to-project memory loop with an integration regression.

Production changes:

- Provenance keeps explicit URL citations exact, but bare site-domain mentions
  such as `python.org` are allowed when Codey opened a child host such as
  `docs.python.org`.
- URL spans are excluded from bare-domain scanning so paths like `pathlib.html`
  are not misread as source domains.
- Verified project work now has direct integration coverage for implementation
  notes, verification notes, and `implements` / `verifies` links back to the
  research synthesis.
- Removed a stale unused `EvidencePack` import from `task_runner.py`.

Validation:

```text
python -m unittest tests.test_server.SessionThreadingTests.test_verified_project_run_records_implementation_and_verification_memory -v: passed
python -m unittest tests.test_research tests.test_server tests.test_ui tests.test_knowledge tests.test_project_task_context: 208 tests OK
python -m unittest discover: 1157 tests OK
python -m py_compile codey\research\provenance.py codey\research\report_quality.py codey\research\ledger.py codey\research\tools.py codey\research\runner.py codey\task_runner.py: passed
python -m ruff check codey tests: passed
git diff --check: passed
```

## 0.2.2 Research Report Quality Gate

Codey 0.2.2 turns Research output into an auditable report pipeline instead of
an unstructured sourced answer.

Production changes:

- Research writes a per-run Evidence Ledger with search queries, ranked search
  results, opened requested/final URLs, source-quality hints, short snippets,
  citation maps, counterpoints, and quality warnings.
- `report_quality.py` is the single deterministic report gate. It requires the
  final report to include conclusion, evidence, counter-evidence/limitations,
  source quality, search coverage, and source sections before saving a
  synthesis note.
- `provenance.py` owns opened-source provenance checks. The old
  `evidence_review.py` compatibility layer was removed.
- The final report can cite only sources opened as final URLs in the run, and
  each cited source must have snippet-backed evidence. Numbered headings and
  Markdown-link source rows are accepted without relaxing provenance.
- Unreadable sources such as PDFs become neutral `SKIPPED` tool results, so
  Research can continue with readable HTML sources. Paraphrased evidence
  excerpts are replaced with exact opened-page snippets and saved with a
  warning.
- The Research drawer is canonicalized around `Evidence`, `Sources`, and
  `Notes`; search coverage is shown inside `Evidence`, leaving room for a
  future graph view.
- Project handoff carries citation maps, evidence snippets, counterpoints, and
  source-quality risks through a bounded Research Brief. Verified project work
  records implementation and verification notes, linked back to the research
  synthesis.

## 0.2.1 Research Polish and UI Follow-through

Codey 0.2.1 tightens the Research and Local-model UX added in 0.2.0 without
changing the explicit Research boundary.

Production changes:

- Local model configuration no longer exposes a `Clear saved key` checkbox.
  Blank API key input preserves the saved key; entering a new key replaces it.
- `NEEDS_OPEN` is now a neutral Research tool status for "open this source
  before saving a note". It does not write a note, does not mark the tool as
  changed, and is carried through both generic run-event payloads and the
  Web/SSE `TaskRunner._ui_event()` path.
- Active `Research` text is brighter only, with no border, background, or
  font-weight change.
- Assistant replies render expanded by default; long replies offer a `Collapse`
  action.
- Assistant Markdown rendering supports `#` through `######` headings and
  basic nested lists.

Validation:

```text
python -m unittest tests.test_server tests.test_ui: 153 passed
python -m unittest tests.test_browser tests.test_research tests.test_handoff tests.test_server tests.test_providers tests.test_ui: 252 passed
python -m unittest discover: 1141 passed
python -m py_compile codey\task_runner.py tests\test_server.py: passed
Node parse codey\web\index.html script: ok
stale browser/clear-key runtime string scan: no results
git diff --check: passed
```

## 0.2.0 Research, Knowledge, and Local Models

Codey 0.2.0 adds an explicit Research work loop, a local knowledge vault, a
bounded Research-to-Project handoff, and a Local OpenAI-compatible provider.
This is the first release where Codey can research a topic, save grounded
notes, carry conclusions into a project Writer, and record verified
implementation facts without copying source code into the vault.

Production changes:

- `knowledge/` stores Markdown notes, note links, SQLite FTS search, per-run
  restore snapshots, and bounded Research Briefs for project handoff.
- `research/` adds the Research runner, JSON research tools, browser search and
  page open, URL policy, source extraction, evidence review, and read-only
  advisor packets.
- `task_runner.py` routes explicit `Research` and `hybrid` runs through
  `ResearchRunner`, injects bounded research handoff for continuous Research
  and Project Hybrid work, and keeps project Writers limited to a Research
  Brief rather than the full vault.
- `providers/local_openai.py` adds the `Local` provider for OpenAI-compatible
  endpoints with base URL/model/API-key configuration.
- `browser_worker.call()` is reentrant so Research browser tools cannot deadlock
  when the task itself is already running on the browser worker.
- Web providers keep Playwright `send()` calls on the browser-worker thread.
  Only `thread_safe_send` providers such as `LocalOpenAIProvider` use the
  cancellable background-send path.
- Hidden-browser runtime paths were removed; Codey reuses or launches the
  normal visible CDP browser.
- The web UI adds a lightweight composer context:
  `Choose folder · Research`, plus a Research drawer and bottom provider picker
  for model selection / Local configuration.

Validation:

```text
python -m unittest tests.test_server tests.test_research tests.test_handoff: 131 passed
python -m unittest tests.test_browser tests.test_research tests.test_handoff tests.test_server tests.test_providers tests.test_ui: 245 passed
python -m unittest discover: 1134 passed
python -m py_compile codey\browser_worker.py codey\browser.py codey\providers\local_openai.py codey\server.py codey\research\browser_search.py codey\research\runner.py codey\handoff.py codey\task_runner.py: passed
Node parse codey\web\index.html script: ok
browser launch cleanup scan: no stale runtime path
git diff --check: passed
```

## 0.1.63 Single Provider Self-Review

Final diff review now has a same-provider fallback. Codey still prefers a
different open reviewer provider, but when all external reviewers are
unavailable it opens the Writer provider in a temporary fresh tab and runs a
clearly labelled self-review pass.

Production changes:

- `providers.registry.connect_fresh_provider_tab()` opens a background fresh tab
  for isolated review-style work while preserving provider-worker overrides.
- `server._run_review()` now tries external reviewers first, then falls back to
  Writer-provider self-review. External reviewer sessions may still be cleared;
  self-review does not clear the Writer provider session.
- `server._run_review_attempt()` centralizes review prompt/send/parse/event
  logic and closes reviewer connections in a `finally` block.
- Cancellation is checked again immediately before opening the self-review
  fresh tab, so a stop request between external-review failure and fallback
  does not create another temporary page.
- Writer repair follow-up wording is neutral: it says a review pass inspected
  the diff, not that a second model did.

Validation:

```text
python -m pytest tests\test_providers.py tests\test_review.py tests\test_server.py tests\test_review_coordinator.py -q: 152 passed, 4 subtests passed
python -m py_compile codey\__init__.py codey\providers\registry.py codey\providers\__init__.py codey\server.py codey\review.py: passed
python -m ruff check codey tests: passed
git diff --check: passed
python -m pytest -q: 1131 passed, 111 subtests passed
```

## 0.1.62 Review Impact Map

Final diff review now receives a short, bounded Review Impact Map after the
ChangeSet summary and before the raw diff. The map lists obvious changed
symbols, local caller reference hints, and local test reference hints so the
Reviewer can inspect likely blast radius without a graph database or new UI.

Production changes:

- `changed_symbols.py` centralizes lexical changed-symbol extraction from the
  collected changes payload, including rename old-name lookup and ChangeSet path
  normalization.
- `verification_map.py` reuses the shared changed-symbol helper, so rename cases
  can find tests that still reference the old symbol name.
- `review_impact_map.py` builds a review-only, best-effort, bounded map from
  changed symbols and `find_reference_hints()`. It keeps only symbol/path/line
  metadata, reserves test-reference slots when available, prefers test hints
  during final bounding, uses a symbol-aware fair cap for rendered references,
  and never includes source bodies.
- `review.py` accepts `review_impact_map` as an explicit prompt input and keeps
  the changed-files-only finding rule unchanged.
- `server.py` computes the map through `safe_review_impact_map()` before sending
  review prompts. Failures return an empty map and do not block review.

No Writer behavior, UI, tools, provider logic, runtime permissions, or
`/api/changes` output changed.

Validation:

```text
python -B tests\manual\review_impact_map_ab.py --self-test: passed
python -m pytest tests\test_change_set.py tests\test_changed_symbols.py tests\test_review_impact_map.py tests\test_review_impact_map_ab.py tests\test_review.py tests\test_review_coordinator.py tests\test_verification_map.py tests\test_server.py tests\test_task_runner_project_map.py tests\test_work_checkpoint_flow.py -q: 183 passed
python -m ruff check codey tests: passed
python -m py_compile codey\__init__.py codey\change_set.py codey\changed_symbols.py codey\review_impact_map.py codey\verification_map.py codey\review.py codey\server.py tests\manual\review_impact_map_ab.py tests\test_change_set.py tests\test_changed_symbols.py tests\test_review_impact_map.py tests\test_review_impact_map_ab.py: passed
git diff --check: passed
python -m pytest -q: 1125 passed, 111 subtests passed
```

Live web-model Review A/B was run one provider per process against the Codey
CDP browser on port 9222, with a 90-second timeout per send:

```text
python -B tests\manual\review_impact_map_ab.py --provider deepseek --timeout 90
python -B tests\manual\review_impact_map_ab.py --provider qwen --timeout 90
python -B tests\manual\review_impact_map_ab.py --provider mimo --timeout 90
python -B tests\manual\review_impact_map_ab.py --provider glm --timeout 90
```

Across DeepSeek, Qwen, MiMo, and GLM on three seeded cases each, both arms
caught the intended correctness issue whenever one existed: `issue_hit` stayed
12/12 for both current and impact-map prompts. The impact-map arm improved
specific affected-caller mentions from 0/8 to 8/8 and relevant test mentions
from 2/4 to 4/4, with no false-positive review on the safe private-helper
control case. Average quality score rose from 4.833 to 6.333. The prompt grew
by about 549 characters on average.

Decision: ship the review-only slice. The measured gain is better reviewer
specificity around affected callers and tests, not better raw bug detection, so
the map stays advisory and explicitly labelled as not coverage proof.

## 0.1.61 ChangeSet Anchored Review

Final diff review now receives a structured ChangeSet summary before the raw
diff, and reviewer findings can carry optional, validated anchors. This gives
the Reviewer a file/hunk map and gives the Writer better repair clues while
keeping path-only findings valid for model output that does not include anchors.

Production changes:

- `change_set.py` adds a bounded ChangeSet interpretation layer over the
  existing `changes.py` dict output. It parses unified diff hunk headers,
  renders a structure-first summary, and validates anchors without changing the
  existing changes payload.
- `review.py` accepts optional `hunk_index`, `new_line`, and `old_line` fields
  on findings, validates them against the current ChangeSet when available, and
  renders anchored review follow-ups as clues rather than facts.
- `server.py` passes the reviewed `changes` payload into review parsing so
  production reviewer anchors are cleaned before any repair turn reaches the
  Writer.
- Git rename labels such as `old.py -> new.py` are normalized inside ChangeSet
  only, preserving old `/api/changes` behavior while attaching hunks to the new
  path for review.

Validation:

```text
python -B tests\manual\changeset_review_ab.py --self-test: passed
python -m pytest tests\test_change_set.py tests\test_review.py -q: 25 passed
python -m pytest tests\test_review_coordinator.py tests\test_server.py -q: 106 passed
python -m pytest tests\test_changes.py tests\test_task_runner_project_map.py tests\test_work_checkpoint_flow.py tests\test_project_task_context.py -q: 45 passed
python -m pytest tests\test_cli.py tests\test_agent.py tests\test_protocols.py -q: 130 passed, 25 subtests passed
python -m ruff check codey tests: passed
python -m pytest -q: 1103 passed, 111 subtests passed
git diff --check: passed
```

Live web-model Review A/B:

```text
python -B tests\manual\changeset_review_ab.py --provider deepseek --timeout 90: baseline avg 4.5, current avg 7.0
python -B tests\manual\changeset_review_ab.py --provider qwen --timeout 90: baseline avg 5.0, current avg 7.0
python -B tests\manual\changeset_review_ab.py --provider mimo --timeout 90: baseline avg 5.0, current avg 7.0
python -B tests\manual\changeset_review_ab.py --provider glm --timeout 90: baseline avg 5.0, current avg 7.0
```

Across DeepSeek, Qwen, MiMo, and GLM on two seeded review cases each,
baseline caught the issue in 8/8 runs and current caught the issue in 8/8
runs. Current improved path correctness from 7/8 to 8/8 and valid anchor
production from 0/8 to 8/8. No provider timed out or required page debugging
during this run.

## 0.1.60 CLI Agent JSONL

CLI agent mode now has an opt-in machine-readable output path. `python -m
codey agent --json ...` writes one JSON object per stdout line for scripts,
CI wrappers, benchmark harnesses, and external launchers, while the default
human-readable CLI behavior remains unchanged.

Production changes:

- `events.run_event_payload()` renders `RunEvent` objects as compact JSON-ready
  payloads, including status/info events, turn events, tool start records, and
  bounded tool result records.
- `codey agent --json` emits a session header, an `agent_start` event, the
  event stream, and an `agent_end` record with final stop reason, summary,
  turn count, change state, and verification flags.
- JSONL payload text fields, command metadata, tool result previews, and final
  summaries are clipped so one event cannot grow into an unbounded stdout line.
- Provider, server, UI, agent runtime, and tool execution behavior are
  unchanged.

Validation:

```text
python -m pytest tests\test_cli.py tests\test_agent.py tests\test_protocols.py -q: 130 passed, 25 subtests passed
python -m ruff check codey\cli.py codey\events.py tests\test_cli.py: passed
python -m py_compile codey\cli.py codey\events.py: passed
python -m ruff check codey tests: passed
git diff --check: passed
python -m pytest -q: 1092 passed, 111 subtests passed
```

## 0.1.59 Package Manager Setup Hints

Setup context and shell follow-up now share command-formatting and package
manager selection with trusted verification discovery. This keeps install and
follow-up hints from drifting away from the commands Codey would trust for
verification.

Production changes:

- `verification_policy.node_package_manager_for_directory()` exposes the
  existing package-manager selection rule without checking executable
  availability.
- `setup_context.py` uses that rule for Node install hints, including parent
  lockfiles and `packageManager` overrides.
- `shell_followup.py` renders trusted check candidates via
  `verification_candidate_lines()`, so cwd-scoped hints use the same format as
  Project Map and Verification Map.

Validation:

```text
python -m pytest tests\test_verification_policy.py tests\test_setup_context.py tests\test_shell_followup.py -q: 62 passed, 5 subtests passed
python -m pytest tests\test_server.py tests\test_shell_risk.py -q: 100 passed, 39 subtests passed
python -B tests\manual\shell_approval_followup_ab.py --self-test: passed
python -m ruff check codey tests: passed
python -m py_compile codey\verification_policy.py codey\setup_context.py codey\shell_followup.py: passed
git diff --check: passed
python -m pytest -q: 1089 passed, 111 subtests passed
```

## 0.1.58 Scoped Successful Change Checks

Successful-change project facts now preserve the working directory for the
checks that justified the change. A scoped run such as `npm test` from
`backend/` is stored and rendered as `backend/: npm test`, instead of being
persisted as a root-level command with no path context.

Production changes:

- `ProjectFactsStore.record_successful_change()` accepts structured check
  evidence with both `command` and `cwd`.
- New `successful_changes[].checks` payloads are written as `{command, cwd}`.
- Legacy string-only check payloads remain readable and are treated as
  project-root checks.
- TaskRunner passes `ExecutionEvidence.successful_checks` through without
  stripping `cwd`.

Validation:

```text
python -m pytest tests\test_project_facts.py tests\test_work_checkpoint_flow.py tests\test_project_task_context.py tests\test_verification_policy.py tests\test_server.py -q: 175 passed, 5 subtests passed
python -m ruff check codey tests: passed
python -m py_compile codey\project_facts.py codey\task_runner.py: passed
git diff --check: passed
python -m pytest -q: 1085 passed, 111 subtests passed
```

## 0.1.57 Policy-Sourced Verification Candidates

Codey now has a single source of truth for trusted local check candidates.
`ProjectTaskContextBuilder` discovers commands through `verification_policy`
and injects those explicit lines into Project Map. Direct `render_project_map()`
continues to render bounded project structure, but no longer guesses candidate
commands from manifests by itself.

Production changes:

- Project Map candidate commands now come from the same trusted policy discovery
  used by the Writer completion gate.
- Review Verification Map shows only the uniquely selected, change-relevant
  candidate under `Recommended local check candidates`.
- When there is no unique selected candidate, Review keeps the weaker `Broader
  check candidates` wording instead of over-recommending unrelated monorepo
  commands.
- Manual probes that evaluate production Project Map now use a shared
  `ProjectTaskContextBuilder` helper instead of direct `render_project_map()`.

Validation:

```text
python -m pytest tests\test_project_map.py tests\test_project_task_context.py tests\test_manual_project_task_context.py tests\test_task_runner_project_map.py tests\test_verification_map.py tests\test_server.py tests\test_verification_policy.py -q: 180 passed, 5 subtests passed
python -B tests\manual\default_verification_ab.py --self-test: passed
python -B tests\manual\task_lens_ab.py --self-test: passed
python -B tests\manual\zoom_project_map_ab.py --self-test: passed
python -m ruff check codey tests tools: passed
python -m py_compile codey\verification_policy.py codey\project_map.py codey\project_task_context.py codey\verification_map.py codey\task_runner.py tests\manual\project_task_context.py: passed
git diff --check: passed
python -m pytest -q: 1081 passed, 111 subtests passed
```

## 0.1.56 Composer Folder Label Cleanup

The no-project composer context now always shows the shorter `Choose folder`
label. Draft-to-project send still uses the same explicit folder click, but the
visible composer chrome no longer switches to the longer `Choose folder to send`
wording.

Validation:

```text
python -m pytest tests\test_ui.py -q: 46 passed
python -m pytest tests\test_ui.py tests\test_handoff.py tests\test_server.py tests\test_ui_browser_e2e.py -q: 150 passed
python -m ruff check codey\handoff.py tests\test_ui.py tests\test_handoff.py tests\test_server.py tools\ui_e2e.py: passed
python -m py_compile codey\handoff.py: passed
git diff --check: passed
```

## 0.1.55 Draft-to-Project Send

Codey now lets a no-project chat become a project task only through an explicit
folder choice in the composer context. This keeps normal chat free of project
access while making the common "discuss first, then apply it here" path direct.

Production changes:

- The composer context now provides a `Choose folder` affordance when a chat has
  no project. It is clickable only while idle and only when the active session
  has no resolvable project.
- `Add project` attaches the current no-project chat in place instead of always
  creating a new chat. Stale `projectId` values are treated as no resolvable
  project, so those chats remain visible under `CHATS` and can be attached
  again.
- Draft-to-project sending uses a stable `sessionId`, clears the draft only
  after local send start, and leaves the draft alone if another run starts or
  the session changes while the folder picker is open.
- Chat-to-project transitions preserve the prior conversation handoff and a
  bounded visible excerpt, so the Writer receives the discussion facts that led
  to the project task.

Safety and scope:

- No natural-language or keyword intent detector was added.
- Pressing Enter in a no-project chat remains a normal chat send.
- No model tool, shell permission, provider flow, or project access policy was
  changed.

Validation:

```text
python -m pytest tests\test_server.py::SessionThreadingTests::test_restart_after_chat_attach_preserves_project_handoff_for_writer -q: 1 passed
python -m pytest tests\test_ui.py -q: 46 passed
python -m pytest tests\test_ui.py tests\test_handoff.py tests\test_server.py tests\test_ui_browser_e2e.py -q: 150 passed
python -m ruff check codey\handoff.py tests\test_ui.py tests\test_handoff.py tests\test_server.py tools\ui_e2e.py: passed
python -m py_compile codey\handoff.py: passed
python -m pytest -q: 1071 passed, 111 subtests passed
git diff --check: passed
```

The restart regression covers this path: a New Chat with planning facts is
attached to a project, the UI state is persisted, Codey restarts, and the first
project Writer still receives the factual handoff plus bounded visible chat
excerpt. The current request is not duplicated into the handoff, and tool/shell
outputs from the visible chat are not included.

## 0.1.54 Trusted Verification Discovery

Codey now discovers a wider set of trusted verification commands that were
already permitted by the local `run` tool, without adding UI, shell permissions,
automatic installs, or automatic execution behavior.

Production changes:

- `verification_policy.py` now discovers package scripts selected via
  `packageManager`, current-directory lockfiles, or the nearest parent lockfile;
  `[tool.pytest.ini_options]`; `tests/` unittest discovery; `ruff`/`mypy`
  configs; and simple safe Makefile targets.
- Verification selection now includes a command priority so discovering more
  safe checks does not make common projects ambiguous. Package/Python ecosystem
  checks outrank Makefile fallbacks, and build commands remain lowest priority.
- Task receipt verification now accepts successful same-family full-suite
  checks that cover the changed files, while still rejecting scoped or unrelated
  green commands such as `pytest tests/old.py` or `py_compile other.py` in place
  of a selected pytest/unittest check.
- Follow-up review fixes aligned the Agent default verification gate with the
  same full-suite replacement policy, recognized `bun test` as a Bun test-family
  command without requiring a package script, and prevented Makefile variable
  assignments such as `check := ...` from being discovered as targets.
- The same-family replacement rule is intentionally conservative: full-suite
  commands such as `python -m pytest` may satisfy a discovered unittest
  candidate, but scoped commands such as `python -m pytest tests/old.py` do not
  claim coverage for unrelated changed source files. Pytest replacement only
  allows a small output/verbosity flag whitelist; selection and exclusion flags
  such as `--ignore`, `-k`, `-m`, and `--collect-only` are not treated as
  full-suite verification. Manual default-verification A/B accounting uses the
  same helper.

Validation:

```text
python -B -m unittest tests.test_verification_policy: 35 tests OK
python -B -m unittest tests.test_shell_followup tests.test_work_checkpoint_flow: 27 tests OK
python -B -m unittest tests.test_agent tests.test_tool_runtime tests.test_project_task_context tests.test_server: 258 tests OK
python -B tests\manual\default_verification_ab.py --self-test: passed
python -B -m py_compile codey\agent.py codey\verification_policy.py codey\task_runner.py tests\test_agent.py tests\test_verification_policy.py tests\test_work_checkpoint_flow.py tests\manual\default_verification_ab.py: passed
python -B -m pytest -q: 1064 tests OK, 111 subtests OK
git diff --check: passed
```

## Post-0.1.53 Impact Guard Probe - Not Shipped

`tests/manual/impact_guard_ab.py` was added as a probe-only A/B harness for a
post-edit Impact Guard. The guard arm wraps `edit_file` only inside the manual
script: after an edit changes a small set of function, class, export, or
constant definitions, it runs a bounded read-only lexical reference scan and
appends a short `path:line` note to the edit result. The note always says it is
not coverage proof and does not include source bodies.

This probe did not change production code, prompts, protocols, UI, automatic
verification, or runtime tool permissions.

Validation:

```text
python -B tests\manual\impact_guard_ab.py --self-test: passed
python -B -m py_compile tests\manual\impact_guard_ab.py: passed
python -B -m unittest tests.test_agent tests.test_tool_runtime: 156 tests OK
```

Live A/B across DeepSeek, MiMo, Qwen, and GLM showed mixed but useful evidence:

- `python-function-rename`: both arms succeeded for all four providers. The
  guard arm reduced turns/tool calls for DeepSeek, MiMo, and Qwen, while GLM was
  neutral.
- `ts-exported-function-rename`: strong positive signal. The current arm missed
  the `src/view.ts` caller for all four providers; the guard arm fixed it for
  DeepSeek, MiMo, and Qwen. GLM still failed, though with fewer turns/tools.
- `public-string-control`: DeepSeek, MiMo, and Qwen did not over-rename the
  public string contract. GLM's guard arm failed before any guard was generated
  or exposed, so it was recorded as a provider/protocol failure rather than an
  Impact Guard regression.

Decision: keep the probe and result notes for future refactor testing, but do
not promote Impact Guard to production on the current evidence. The strongest
win was a TypeScript export case, which is not the primary local usage target
for the current project. The Python rename case was already solved by the
current stack, and the sample set is still too small to justify adding a new
post-edit production prompt path.

## 0.1.53 CDP Browser Warmup

Codey UI startup now schedules a best-effort provider browser warmup on the
shared browser worker. The warmup prepares the durable Codey-controlled CDP
browser and opens DeepSeek, Qwen, MiMo, and GLM provider pages when no provider
tab is already visible.

Safety boundaries for this patch:

- No login-state check.
- No test message is sent.
- No UI changes or onboarding flow changes.
- Provider availability exposed to the UI still goes through provider supervisor
  health filtering.
- Warmup reuses remembered Codey CDP ports only; it does not reuse unrelated
  external CDP browsers.
- Warmup uses short best-effort timeouts and closes failed blank tabs while
  keeping slow provider pages that reached the target URL.

Validation for this patch:

```text
focused pytest: 135 passed, 3 subtests passed
full pytest: 1040 passed, 106 subtests passed
ruff: All checks passed
git diff --check: passed
```

Live smoke on 2026-07-17:

```text
no Edge / no CDP: warmup opened DeepSeek, Qwen, MiMo, and GLM provider pages
existing Codey CDP with about:blank: warmup opened all four provider pages
ordinary Edge about:blank without CDP: warmup started Codey CDP and opened all four provider pages
existing provider pages: warmup returned existing statuses and did not duplicate tabs
```

## 0.1.52 Provider Send Loop Consolidation

This patch adds `provider_send_loop.py`, a small shared helper module for web
provider send-loop lifecycle concerns: response-watch lifetime, response
stability state, completion-flow checks, flow response reads, and standard
timeout recovery. GLM, Qwen, DeepSeek, and MiMo now use the shared helpers,
while provider-specific behavior remains visible inside each web driver.

Safety boundaries for this patch:

- No UI changes.
- No selector changes.
- No provider base class or broad `run_send_flow` callback framework.
- GLM keeps its duplicate-submission guard, rate-limit retry, generation
  completion check, final response reader, and JSON normalization locally.
- Qwen keeps empty-response regeneration and its sanitized click-error
  `SubmissionUncertain` wording.
- DeepSeek keeps JSON-tool stability shortcuts, missing-brace repair,
  rate-limit retry, and its local late-response fallback.
- MiMo keeps typed completion observation and its timeout fallback completion
  gate before response recovery.

Validation for this patch:

```text
provider send loop focused pytest: 63 passed, 4 subtests passed
Qwen focused pytest: 77 passed, 4 subtests passed
DeepSeek focused pytest: 52 passed, 4 subtests passed
MiMo focused pytest: 72 passed, 15 subtests passed
provider recovery focused pytest: 149 passed, 23 subtests passed
combined provider focused pytest: 254 passed, 34 subtests passed
provider send loop focused ruff: All checks passed
ruff: All checks passed
py_compile: passed
full pytest: 1026 passed, 106 subtests passed
git diff --check: passed
GLM live smoke: passed; direct marker send returned the expected nonce in 9.329s
Qwen live smoke: passed; direct marker send returned the expected nonce in 12.090s
DeepSeek live smoke: passed; direct marker send returned the expected nonce in 7.080s
MiMo live smoke: passed; direct marker send returned the expected nonce in 7.274s
```

## 0.1.51 Shell Approval Follow-up

Approved shell continuations now include bounded `Follow-up hints` for the
Writer. These deterministic hints summarize exit status, selected output
signals, ambiguity around long-running dev servers, publish confirmation, and
trusted verification candidates when relevant. They do not run commands, retry
installs, or change UI behavior.

Safety boundaries for this patch:

- Follow-up hints are internal guidance only; Writer must still request tools or
  shell approval explicitly for any next action.
- Generic shell commands do not scan the project for verification candidates.
- Dependency install, dev-server, and publish follow-up hints may use existing
  bounded verification-candidate discovery.

Validation for this patch:

```text
shell follow-up focused pytest: 100 passed
focused pytest: 146 passed, 64 subtests passed
ruff: All checks passed
py_compile: passed
full pytest: 1004 passed, 106 subtests passed
git diff --check: passed
live provider AB: DeepSeek/MiMo/Qwen/GLM completed baseline+full prompts
```

Post-release live A/B on 2026-07-17 compared the old approved-shell
continuation shape (`baseline`) with the full setup-aware continuation plus
follow-up hints (`full`). The probe used synthetic shell results only; it did
not execute install, clone, publish, or dev-server commands.

| Provider | Baseline safe | Full safe | Baseline bad claims | Full bad claims | Notes |
| --- | ---: | ---: | ---: | ---: | --- |
| DeepSeek | 4/4 | 4/4 | 0 | 0 | Already cautious; full gave a concrete pytest step after push. |
| MiMo | 3/4 | 4/4 | 1 | 0 | Full fixed the install-success case: baseline said project was ready; full asked to run build/test. |
| Qwen | 4/4 | 4/4 | 0 | 0 | Already safe; full made install/publish verification steps more concrete. |
| GLM | 4/4 | 4/4 | 0 | 0 | Already safe after scorer correction; full suggested a trusted check after push. |

Aggregate rescored result:

```text
baseline: 15/16 semantic_safe, 1 unsupported success claim
full:     16/16 semantic_safe, 0 unsupported success claims
protocol failures: 0
web blockers: 0
```

One scorer bug was found during the probe: the initial detector treated
"verify the project is ready" as a claim that the project was already ready.
The probe and unit tests were corrected so verification-next-step language is
scored as safe.

## 0.1.50 Setup-Aware Shell Approval

Shell approval now carries neutral risk explanations for dependency installs,
system installs, external-source retrieval, publishing, dev servers, and generic
shell commands. After the user approves setup-like shell commands, the
continuation prompt includes a bounded read-only `Setup Context` with local tool
availability, project manifests, lockfiles, scoped setup notes, and omission
signals. This context is not exposed as a new model tool and is not sent in
normal task prompts.

Safety boundaries for this patch:

- Setup context only runs after approved setup-like shell commands.
- It never installs, clones, writes files, or performs network access itself.
- It does not expose absolute tool paths.
- It reuses sensitive-path filtering and skips dot/secret/private/token paths.
- Manifest listing caps and bounded scan truncation are explicit.

Validation for this patch:

```text
setup/shell focused pytest: 13 passed, 39 subtests passed
focused pytest: 186 passed, 64 subtests passed
ruff: All checks passed
py_compile: passed
full pytest: 990 passed, 106 subtests passed
git diff --check: passed
live provider smoke: not run; this is a local approval/continuation/UI slice
```

## 0.1.49 Tool Start Visibility

Agent tools now emit a lightweight `tool_started` event immediately before
local execution begins. The web UI renders that event as a pending `.tool-line`
and replaces it with the final `tool` event by matching `tool_id`.

This preserves Codey's serial execution model: no production read-only
concurrency, no progress framework, and no ToolSpec registry were added.
`tool_started` is UI/CLI visibility only; `TaskRunner` deliberately skips it
for execution evidence, reviewer recent logs, project facts, and checkpoint
updates.

Validation for this patch:

```text
ruff: All checks passed
ui static pytest: 41 passed
focused pytest: 216 passed
full pytest: 971 passed, 67 subtests passed
live provider smoke: MiMo/Qwen read_files+parallel passed;
  MiMo read_files had one transient timeout after a complete ordered tool
  trace, then passed on retry with a longer provider timeout
```

## 0.1.48 Tool Function Injection and Parallel Probe

Agent runtime now supports explicit `AgentToolFns` injection, so tests and
manual probes can replace tool functions without monkeypatching `codey.agent`
globals. Production tool execution remains serial by default, including
`read`, `ls`, and `search`, to preserve observable step-by-step tool progress
in the chat stream.

The `readonly_parallel_ab.py` deterministic probe remains as a script-local
experiment. It preserves evidence that read-only concurrency can improve local
wall-clock time, while documenting the product decision not to enable it by
default because Codey values quiet observability over this small optimization.

Cancellation coverage was extended so bounded scan/search loops check
cooperative cancellation during long traversal.

Validation for this patch:

```text
ruff: All checks passed
focused pytest: 107 passed
readonly_parallel_ab deterministic probe: passed
full pytest: 970 passed, 67 subtests passed
live provider smoke: DeepSeek/MiMo/Qwen/GLM read_files+parallel passed;
  DeepSeek parallel had one transient timeout before retry, and GLM passed
  after an earlier rate-limit/sliding-verification block cleared
```

## 0.1.47 Search Coverage Bugfix

Writer `grep` / `search` now reports non-UTF-8 and unreadable files instead of
silently skipping them and returning a clean-looking no-match result. The fix is
local to `tool_runtime.search_files()`: oversized files, read-budget stops, and
bounded scan-budget stops keep their existing messages, and hidden advisor
search remains unchanged.

Live A/B on 2026-07-16 for `search-non-utf8-omission`:

```text
baseline: safe=2/4, bad_confident_absence=2, false_scan_complete_claim=2,
          13 turns, 8 tool calls, 4 searches
coverage: safe=4/4, bad_confident_absence=0, false_scan_complete_claim=0,
          10 turns, 6 tool calls, 6 searches
```

Production smoke after the patch used Qwen against a temporary project with one
non-UTF-8 file containing the marker. The real `search_files()` result was
marked `truncated=True`, included `Scan coverage`, and Qwen answered that the
search was incomplete instead of claiming definite absence.

Validation for this patch:

```text
search_coverage_ab self-test: passed
focused pytest: 165 passed
full pytest: 959 passed
full unittest: 936 tests OK
ruff: All checks passed
Qwen production smoke: done in 2 turns, no changes
```

## Post-0.1.46 Task Lens Probe - Not Shipped

A probe-only `task_lens_ab.py` benchmark was added to test whether a compact
Coverage-aware Task Lens should replace the production task-aware Project Map
navigation block. The `current` arm is the 0.1.46 production
`render_project_map(..., task=task)` output; the `lens` arm replaces Focused
subtree / Symbol overview with a short Task Lens prototype.

Live file-pick A/B on 2026-07-16 across DeepSeek, MiMo, Qwen, and GLM showed no
navigation lift because the production Focused subtree was already saturated:

```text
current: 16 rows, 32 path hits, 16 test hits, 16 top1 path hits
lens:    16 rows, 32 path hits, 16 test hits, 16 top1 path hits
prompt:  lens was 560 total characters shorter
```

Live read-only agent A/B across Qwen, MiMo, and GLM on two unnamed deep cases
each showed a regression:

```text
current: 6/6 correct, 6/6 first-read hits, 18 tool calls, 0 searches, 19 turns
lens:    5/6 correct, 6/6 first-read hits, 24 tool calls, 1 search, 19 turns
```

DeepSeek read-only hit a provider `rate_limited` send failure and was excluded
from the paired readonly aggregate. GLM's failed lens case had already read the
right files, then stopped without valid tool progress, so it was recorded as a
real lens-arm protocol/navigation regression rather than a DOM blockage.

Decision: keep Task Lens as a manual regression probe only. Do not change
production Project Map output without a stronger fixture or live result that
beats the 0.1.46 Focused subtree baseline on first-read hits, correctness, or
tool count.

## 0.1.46 Coverage-Aware References

`find_references` now exposes Writer-facing scan coverage when the bounded
reference scan skips files that may contain additional references. The low-level
reference scanner collects `ScanReport` facts without rendering them, so hidden
project-audit advisors keep their previous output. The Writer tool wrapper
renders a short coverage note and marks the tool result as truncated, reusing
the existing JSON protocol warning for omitted content.

Covered cases:

- oversized files are counted and reported with at most three path examples;
- skipped oversized files do not leak source bodies;
- non-UTF-8 files and unreadable files are recorded as incomplete scan facts;
- low-level `find_reference_hints` output remains unchanged for advisor paths;
- hidden project-audit references do not render Writer coverage;
- the scan-coverage A/B baseline reconstructs the old low-level output so the
  probe stays meaningful after production coverage is enabled.

Live scan-coverage A/B rerun on 2026-07-16, one provider at a time against Edge
CDP on port 9222:

```text
DeepSeek baseline: safe, 2 turns, 1 tool, 0 search
DeepSeek coverage: safe, 2 turns, 1 tool, 0 search

MiMo baseline: safe, 4 turns, 3 tools, 1 search
MiMo coverage: safe, 3 turns, 2 tools, 1 search

Qwen baseline: safe, 3 turns, 2 tools, 1 search
Qwen coverage: safe, 2 turns, 1 tool, 0 search

GLM baseline: unsafe; bad_confident_absence=true,
              false_scan_complete_claim=true
GLM coverage: safe; incomplete scan and skipped oversized file were explicit
```

Validation for the final pre-commit state:

```text
Focused coverage/advisor/unit tests: 98 tests OK
Full unittest: 926 tests OK
Full pytest: 944 passed
Ruff: All checks passed
scan_coverage_ab self-test: passed
git diff --check: passed
```

## 0.1.45 Provider Adapter Self-Repair

Provider adapter self-repair now has an end-to-end bounded path: structural
provider failures can enqueue a deduplicated repair job; the repair subprocess
asks healthy helper providers to modify only the broken adapter files in a
sandbox; policy/static/provider tests and a neutral marker canary gate the
candidate; and the enabled candidate runs only through a child Provider worker.

The final browser strategy uses a fresh background tab in the durable logged-in
Codey browser profile. Earlier isolated-profile auth bootstrapping was removed
because Qwen could appear logged in while still failing to submit. The child
worker reports the fresh tab target id, allowing the parent to close that tab
before killing a stuck worker.

Additional edge cases covered:

- invalid helper output / policy failure / test failure / canary failure tries
  the next helper instead of ending the repair;
- failed repairs stay queued behind a cooldown instead of disappearing;
- generated tests are no longer allowed; provider tests are read-only
  references;
- enabled overrides are invalidated by Codey base-hash drift;
- candidate overrides do not shadow an existing active override until canary
  passes and the candidate is marked provisional;
- rollback does not restore a missing previous generation.

Live smoke on the final shared-profile fresh-tab path:

```text
DeepSeek: fresh helper OK, candidate worker canary OK
Qwen:    fresh helper OK, candidate worker canary OK
MiMo:    fresh helper OK, candidate worker canary OK
GLM:     fresh helper OK, candidate worker canary OK
```

Validation for the final pre-commit state:

```text
Focused self-repair/browser/provider/cancellation unittest: 96 tests OK
Full unittest: 908 tests OK
Full pytest was intentionally skipped for the final doc/type cleanup per user request
Ruff focused/full checks: passed
compileall: passed
git diff --check: passed
```

## 0.1.44 Focused Project Map

Project Map now includes a bounded `Focused subtree` section for larger
task-aware repositories. It scans source files under fixed file, directory,
single-file-size, total-byte, and output-character budgets, then emits only
relative paths, source/test labels, and symbol signatures for the top-scored
module. It does not emit source bodies, build an index, persist data, add UI,
or call an extra planning model.

The section appears only when a task is present and only when the normal
task-aware Symbol overview is likely insufficient because the repository is
larger than the symbol scan budget or the focused scan itself hit a budget.
When it appears, it replaces the ordinary Symbol overview so deep navigation
hints are not diluted by low-relevance early files.

Qwen readiness now waits for message input visibility, bootstrap readiness, and
two consecutive identical non-empty model-selector reads before sending. The
final readiness fallback uses the same rule. Qwen submission confirmation no
longer treats a cleared input box as evidence that the message was accepted; it
requires stop visibility or response-count growth.

Two scoped/pre-scope probes were intentionally not promoted. The two-step
hidden Scoped Task Plan arm lost path/test hits and more than doubled prompt
traffic. The lighter deterministic scope hint was neutral on the stockalarm
large-repo cases while adding prompt characters. The retained production path
is the layered map / zoom-map approach.

Live zoom-map A/B across DeepSeek, MiMo, Qwen, and GLM on the deep synthetic
monorepo:

```text
current: top1 0/16, path_hits 0,  test_hits 0,  chars 53,424
zoom:    top1 16/16, path_hits 31, test_hits 16, chars 33,564
```

Qwen submit probe after the readiness fix:

```text
new_chat seconds=7.69
after send attempt seconds=9.39
responses=1
reply={"ok":true}
```

Validation:

```text
Focused pytest: 137 passed, 2 subtests passed
Full pytest: 859 passed, 67 subtests passed
zoom_project_map_ab --self-test: passed
zoom_project_map_ab --max-cases 4 --dry-run: passed
scoped_task_plan_ab --self-test: passed
Ruff, compileall, and git diff --check: passed
```

## Internal ProjectTaskContext Refactor

Project task context preparation is now isolated in
`codey/project_task_context.py`. The new builder prepares verified project
facts, Project Map text, checkpoint resume/start state, checkpoint prompts,
resumed changed files, resumed successful checks, and initial verification
candidates before the Writer runs.

`TaskRunner` still explicitly owns execution evidence seeding/invalidation,
Writer and Review execution, Receipt construction, conversation state, and
Provider failover. This keeps the refactor behavior-preserving while reducing
the amount of checkpoint and ProjectFacts state assembly inside the main task
control flow.

Validation:

```text
ProjectTaskContext focused unittest: 26 tests OK
Focused pytest/server/work checkpoint set: 131 passed
Full pytest: 869 passed, 67 subtests passed
Ruff, py_compile/compileall, and git diff --check: passed
```

## Internal ReviewCoordinator Refactor

Diff Review lifecycle management is now isolated in
`codey/review_coordinator.py`. The coordinator owns the bounded review-time
state machine: retrying unavailable diffs before review, checking whether a
diff is reviewable, handling review-unavailable fallback, turning rejected
reviews into Writer follow-up prompts, marking diff state dirty after repair,
and preserving the narrow checks-passed inheritance rule when a repair confirms
the reviewer finding was invalid without changing files or running checks.

`TaskRunner` still owns reviewer connection, Writer failover, provider state,
Receipt construction, ProjectFacts writes, checkpoint deletion, and conversation
snapshots. This keeps the refactor behavior-preserving while making the
previously fragile diff/review/repair lifecycle independently testable.

Validation:

```text
ReviewCoordinator/server/work checkpoint focused pytest: 119 passed
ReviewCoordinator/server/work checkpoint focused unittest: 110 tests OK
Ruff, compileall, and git diff --check: passed
```

## 0.1.43 Quiet UI Persistence and Sidebar Polish

UI state persistence now separates the SSE hot path from discrete user actions.
`addToSession()` still records every streamed event, but its full localStorage
serialization and `/api/ui_state` POST are debounced. User actions such as
switching chats, creating chats, renaming, deleting, clearing, and provider
selection flush immediately. Terminal task events also flush immediately so the
completed receipt is durable without waiting for the debounce timer.

Sidebar rename now uses inline inputs instead of native `prompt()`. Destructive
actions use a quiet two-step confirmation inside the existing monochrome menu
instead of native `confirm()`. Consecutive read-only tool rows fold at render
time only after a second same-kind row appears; a single read/search/list or
reference row remains visible, while edit, run, shell, and error rows stay
expanded.

Validation:

```text
UI focused unittest: 40 tests OK
Full unittest: 826 tests OK
Full pytest: 834 passed, 67 subtests passed
Ruff, compileall, and git diff --check: passed
```

## Internal Writer Failover Refactor

Writer provider takeover is now isolated in `codey/writer_failover.py`.
`TaskRunner` still owns prompts, verification, review, diffs, receipts, and
project facts; the new runner only coordinates Writer attempts, provider
switches, shared turn budget, canary checks, checkpoint refresh, and Stop
priority. Initial Writer execution and Review repair reuse the same runner
instance so the switch budget and tried-provider set remain shared.

This is an internal maintainability refactor. It does not change the provider
protocol or prompt contract.

Validation:

```text
Focused failover/server/work-checkpoint tests: 107 tests OK
Full pytest: 827 passed, 67 subtests passed
Ruff, py_compile, and git diff --check: passed
```

## 0.1.42 Broader Checks and Quiet Markdown

The controlled `run` allowlist now accepts additional verification commands:
Ruff check and format-check, mypy, safe make targets, Bun test / allowed scripts,
and safe Deno test/lint/check/fmt forms. Mutating and installing variants remain
blocked, including Ruff fix/output-file forms, unguarded Ruff format,
`mypy --install-types`, deploy-style make targets, `bun install`, and
`deno run`.

Suite-style checks now receive a 300-second timeout, while quick commands keep
the existing 90-second timeout. Timeout output explicitly says it is a timeout,
not a test failure, and asks the Writer to rerun a smaller subset instead of
guessing a fix. Literal grep results now add a narrowing hint when they hit the
match cap.

The UI now renders assistant replies with a minimal monochrome Markdown subset:
code blocks, inline code, bold text, simple headings, and simple lists. Code
blocks get a quiet copy button that reuses the existing clipboard helper. The
implementation escapes text before applying inline markup, uses `textContent`
for fenced code, and adds no syntax highlighter or new color palette.

Validation:

```text
Full unittest: 810 tests OK
Full pytest: 818 passed, 67 subtests passed
ruff check .: passed
compileall: passed
```

## 0.1.41 Smart Pagination Hint

Paged `read_file` results now include a concrete next-page JSON call when more
content remains. The existing complete-line paging, `next offset` text, page
metadata format, and truncation semantics remain intact. The added hint uses
the same path, next offset, and effective limit:

```text
next call: {"tool":"read_file","args":{"path":"app.py","offset":301,"limit":300}}
```

The hint is omitted on the final page and is generated with JSON escaping so
Windows-style paths and other escaped characters remain valid JSON. This change
does not touch providers, Agent orchestration, file contents, or verification
logic.

Validation:

```text
Focused runtime/agent/protocol tests: 168 tests OK
Full unittest: 799 tests OK
Full pytest: 807 passed, 67 subtests passed
Ruff, compileall, and git diff --check: passed
```

## 0.1.40 Bounded Stacktrace Pruning

Controlled `run` output now folds obvious dependency stack frames before the
existing `clip_middle()` budget is applied. The pruning is deterministic,
line-oriented, and local to display text: it does not change command execution,
exit codes, `ok`, `changed`, or `truncated` semantics.

Python pruning recognizes explicit traceback frames under `site-packages`,
`dist-packages`, `.venv`, and `venv`, and folds the immediately following
dependency source line with the frame. Node pruning recognizes only explicit
`at ...` stack entries whose parsed location ends with `:line:column` and lives
under `node_modules` or `.pnpm`. Project frames, assertion lines, exception
messages, test names, and ordinary logs are preserved. If no dependency stack
frame is found, the output is returned byte-for-byte unchanged.

No live web-provider smoke was required for this release because the change is
pure local text post-processing after a controlled command completes and before
the existing output clipping step.

Validation:

```text
Focused text-budget/runtime tests: 59 tests OK
Full unittest: 798 tests OK
Full pytest: 806 passed, 67 subtests passed
Ruff, compileall, and git diff --check: passed
```

## Post-0.1.40 Incomplete Refactor Hint Probe

`tests/manual/refactor_hint_ab.py` was added as a probe-only A/B harness. The
hint arm injects an edit wrapper inside the script only:
after a narrow identifier rename, it runs a bounded lexical scan for the old
symbol in other Python/JS/TS source files and appends only a file-count note.
No production code, prompt, protocol, version, or runtime behavior changed.

Live web A/B did not meet the retention threshold. DeepSeek, Qwen, and MiMo
all completed the explicit `python-function-rename` case correctly in both
arms with no missed callers. The shorter `implicit-function-rename` case
showed one positive Qwen sample, improving from 8 turns / 7 tool calls to
5 turns / 6 tool calls, but DeepSeek stayed neutral and MiMo regressed by one
tool call. Qwen's `public-string-control` case showed no over-rename of the
external string contract. Class rename samples for Qwen and DeepSeek were
neutral.

Conclusion: keep the manual probe for future larger-project refactor testing,
but do not promote the hint to production on the current evidence.

Validation:

```text
python -B tests\manual\refactor_hint_ab.py --self-test: passed
Ruff and py_compile for the probe: passed
```

## 0.1.39 MiMo Typing Transition Flow

MiMo now contributes an explicit three-state typing observation to the existing
bounded completion Flow. Deterministic tests cover true-to-false recovery,
thinking pauses, initial false without generation evidence, missing attributes,
DOM errors, built-in completion priority, unreadable final answers, provisional
promotion, rollback, Stop, and deadline propagation.

The live evidence probe stores only bounded booleans, lengths, timings, and
recovery status. It never stores prompt or reply text. All required scenarios
passed:

| Scenario | Result | Evidence |
|---|---|---|
| Short answer | passed | typing transition, stable completion, no later growth |
| Long code | passed | typing remained true during generation, then stable false |
| Deep thinking | passed | empty thinking interval stayed true; no premature completion |
| Forced Flow | passed | two distinct sends, `provisional -> active` |

Observed end-to-end durations were 12.41s, 31.83s, 29.81s, and 20.73s
respectively. The long-code probe was aligned with the production evaluator:
an initial false observation is not enough; completion requires the subsequent
stable non-empty samples used by the real Flow rule.

All browser-visible validation markers now use neutral `SESSION_CHECK_<random>`
values. Temporary DOM attributes, page globals, and clipboard sentinels likewise
contain no product name. Local-only module names, report paths, and environment
variables remain unchanged because web pages cannot observe them.

Final regression:

```text
Focused provider regression: 146 tests OK
Full unittest: 792 tests OK
Full pytest: 800 passed, 67 subtests passed
MiMo probe self-test: passed
Ruff, compileall, residual marker scan, and git diff --check: passed
```

## 0.1.38 Bounded Provider Flow Recovery

Provider recovery now includes one optional single-stage Flow Recipe made only
from fixed boolean observations. Deterministic tests cover schema rejection,
AND evaluation, generation-to-terminal transitions, bounded traces, assistance
suppression, transaction-local attribution, provisional promotion, persistent
failure rollback, profile invalidation, and safe degradation when terminal
evidence is absent. Production-component fault injection additionally covers:

- Qwen recovery when its built-in completion signal fails but a real
  `stop_visible -> stop_hidden` transition remains.
- no completion during a long thinking pause while stop remains visible.
- no guessed completion when the stop marker is also unavailable.
- one failure count per unreadable Flow-selected answer and rollback after the
  second independent structural failure.
- provisional promotion only after the next natural send proves the same rule.
- no MiMo or GLM completion recovery from text stability alone.

The four-provider Edge/CDP control-revival matrix used a temporary store and
in-memory selector faults. Every provider recovered its composer controls,
read both nonce replies, reused the bundle without invoking Doctor again, and
promoted `provisional -> active`:

| Target | First recovery | Persisted reuse | Result |
|---|---:|---:|---|
| DeepSeek | 32.48s | 4.70s | passed |
| MiMo | 21.77s | 12.31s | passed |
| Qwen | 46.92s | 6.95s | passed |
| GLM | 44.72s | 14.41s | passed |

A stricter Qwen live run forced the built-in completion check to remain false
while preserving real stop DOM observations. The first send completed through
the bounded Flow path in 64.34s; the second reused it in 6.06s and promoted the
bundle to active. The first helper could not provide a usable control choice,
so bounded sibling relay continued to MiMo and succeeded without recursion or
duplicate submission.

Final regression after the local fault-injection additions:

```text
Provider/Server focused unittest: 287 tests OK
Full unittest: 781 tests OK
Full pytest: 789 passed, 56 subtests passed
Ruff, compileall, and git diff --check: passed
```

The current completion Flow scope is intentionally narrow: Qwen has reliable
terminal evidence; MiMo and GLM do not yet. This release does not add arbitrary
browser actions, background probing, Python adapter self-modification, or a new
user-visible mode.

## 0.1.37 Python Syntax Regression Hint

A narrow production check compares Python replacement edits before and after
with `ast.parse()`. It emits a bounded navigation hint only when the
original file parsed successfully and the final edited content does not. The
edit remains written and successful; existing-invalid files, non-Python files,
valid edits, and files above the 128K-character parsing budget receive no hint.

The live A/B injected the same missing colon after the first successful target
edit. DeepSeek and Qwen ran baseline first; MiMo and GLM ran hint first to
reduce fixed-order bias. Every fault arm ended with valid syntax, the requested
change, and an independently passing unittest. Every valid-edit control passed
with zero generated or exposed hints.

| Provider | Turns baseline -> hint | Tools baseline -> hint | Runs baseline -> hint | Result |
|---|---:|---:|---:|---|
| DeepSeek | 7 -> 6 | 6 -> 5 | 2 -> 1 | avoided failed run |
| Qwen | 7 -> 6 | 7 -> 5 | 2 -> 1 | avoided failed run and extra read |
| MiMo | 8 -> 6 | 8 -> 6 | 2 -> 1 | avoided failed run and extra read |
| GLM | 7 -> 7 | 6 -> 6 | 2 -> 1 | failed run replaced by one read |

No provider reached max turns or hit a DOM, submission, rate-limit, retry, or
response-timeout failure. All four avoided one failed test run, three reduced
turns or total tool calls, and legal edits produced no false positives. This
meets the predeclared retention threshold for the narrow Python-only behavior;
it does not justify Ruff, JavaScript/TypeScript, LSP, automatic rollback, or
automatic command execution.

## 0.1.36 Provider Revival and Writer Takeover

This release adds atomic provider-control revival, passive provider health,
bounded half-open canaries, and checkpoint-based Writer takeover. Deterministic
tests cover typed failure boundaries, cooldown and restart behavior, Stop
priority, strict fresh-chat takeover, shared turn budgets, stale-check
invalidation, final Diff ownership across Writers, and Review-repair failover.
The four-provider live fault-injection matrix is recorded in the dedicated
section below.

## 0.1.35 Default Post-edit Verification

The default completion boundary now offers one exact trusted check after code
changes when candidate discovery proves a unique runnable command, compatible
ecosystem, and covering working directory. Discovery is bounded to successful
ProjectFacts or checkpoint commands and explicit pytest, npm, Cargo, and Go
configuration. Documentation-only changes, unavailable executables, ambiguous
candidates, and cross-ecosystem commands safely disable the default gate.

Agent regressions cover the per-edit-epoch reminder, proactive green-check
reuse, same-epoch failed-check non-repetition, latest-edit invalidation, exact
candidate matching, and checkpoint reuse. Receipt coverage confirms that a
successful same-ecosystem command cannot stand in for the selected candidate.
Policy tests cover manifest discovery, executable filtering, ecosystem
compatibility, closest monorepo scope, documentation skipping, and cwd
coverage. Candidate directory discovery has both directory and cumulative
entry budgets, skips dot directories and case-insensitive excluded directories,
and safely stops when either budget is exhausted. Production behavior was
first locked locally, then checked with the four-provider smoke recorded below.

Candidate manifests are refreshed from current disk state once per edit epoch
at the completion boundary and once before the final receipt. Historical
ProjectFacts commands are frozen at task start so an unrelated successful run
from the current task cannot promote itself into a trusted candidate. The
manual A/B current arm now calls production discovery, selection, exact-match,
and Agent gate code directly; it contains no duplicate policy or verification
monkeypatch.

Frozen historical and checkpoint commands that depend on manifests are also
revalidated: npm commands require the named current package script, Cargo
requires `Cargo.toml`, and Go requires `go.mod`. Python history remains valid
without a manifest because pytest, unittest, and py_compile can be legitimate
project checks without configuration files.

All verification manifests must be regular project files. Discovery and
historical-command revalidation reject symlinked `package.json`, `pytest.ini`,
`pyproject.toml`, `Cargo.toml`, and `go.mod`, so configuration cannot be
imported through a link to a file outside the selected project.
The read helper also proves the path is a regular file before `stat()` or
`read_text()`, preventing FIFO, socket, device, and directory manifests from
being opened.

Four-provider production smoke used the `python-pytest` current arm after all
policy fixes. Every provider changed only `limits.py`, passed the independent
check, ran the exact selected `python -m pytest` command after the latest edit,
and completed with zero wrong-run attempts:

- DeepSeek: 4 turns, 3 tool calls.
- Qwen: 5 turns, 3 tool calls.
- MiMo: 5 turns, 4 tool calls.
- GLM: 4 turns, 3 tool calls.

No provider hit a DOM submission failure, navigation abort, rate-limit recovery
button, duplicate send, or response timeout during this smoke. Provider code
was therefore unchanged.

A later exploratory A/B tested whether appending bounded verbatim excerpts from
already-visible failed-check output should become a new production feature.
DeepSeek baseline completed in 7 turns / 6 tool calls with 1 read after failure;
the context arm regressed to 8 turns / 7 tool calls with 2 reads after failure.
Both were correct, but the added excerpt supplied no new facts and increased
exploration. A MiMo baseline stalled and was discarded as an invalid sample.
The proposed verification-failure context was therefore withdrawn: no parser,
Agent integration, provider branch, version bump, or permanent probe was kept.

A canonical JSON tool contract fixture now snapshots the ordered `TOOL_SPECS`
fields and the exact rendered contract inserted into the web-model prompt.
Intentional protocol changes must update the reviewed fixture explicitly;
parser behavior tests continue to own required arguments and invalid
combinations. This guard adds no runtime code, prompt text, UI, or model call.

Validation: `python -B -m pytest -q` passed with 654 tests, 8 skips, and 31
subtests. `python -B -m unittest` passed with 654 tests and 8 skips. Full-tree
Ruff, `compileall`, and `git diff --check` passed. Pytest emitted one harmless
warning because the sandbox could not update `.pytest_cache`.

## 0.1.34 Bounded Edit Failure Context

Exact replacement writes remain unchanged. On failure, `edit_file()` may add a
bounded, line-numbered excerpt only when it finds a unique lexical anchor from
the submitted `old_string` in the original disk content. Missing anchors retain
the generic read-again guidance. Multiple exact matches report at most three
start lines. Output is capped at 1,600 characters and seven complete source
lines; overlong lines are omitted with an offset-read instruction.

Atomic multi-replacement failures identify the failed replacement and state
that nothing was written. All failure evidence comes from the original disk
content, never the partially updated in-memory value. Unit coverage includes
stale comments, absent anchors, bounded duplicate positions, omitted matches,
identifier boundaries, quoted literal semantics, long lines, total budget, CRLF
line numbers, late atomic failure, intermediate duplicate matches, unchanged
files, unchanged success output, and tool-result-like source text. A local
100,000-match `edit_file()` regression probe completed in approximately 0.017
seconds and left the file unchanged.

Anchor discovery is capped at 32 deduplicated candidates after a stable
longest-first sort. A local 1,000-candidate probe over approximately 475 KiB
completed in about 0.242 seconds and safely returned no context, compared with
the previously observed multi-second unbounded scan. The JSON tool contract now
states consistently that `content` is only for new-file creation; existing
files must use exact replacements even when JSON escaping is difficult.

Live stale-read A/B used production `edit_file()` for both arms and disabled
only the context renderer for baseline. DeepSeek improved from 6 turns / 5 tool
calls / 1 reread to 5 / 4 / 0. Qwen improved from 7 / 6 / 1 to 5 / 4 / 0.
MiMo improved from 6 / 5 / 1 to 5 / 4 / 0 on the completed comparison; one
separate MiMo context attempt hit a 300-second webpage timeout before a clean
single-arm rerun passed. GLM baseline passed at 6 / 6 / 1. GLM context preserved
correctness and removed the reread, but was variable: one run reached max turns
after producing a correct tested file, and a clean rerun finished at 7 / 5 / 0.
Across completed arms, the target edit was correct, the external comment was
preserved, the unrelated default was unchanged, and local tests passed.

Validation: `python -B -m unittest` passed with 630 tests; `python -B -m pytest
-q` passed with 638 tests and 31 subtests. Full-tree Ruff, `compileall`, manual
probe self-test, and `git diff --check` passed.

## 0.1.33 Read-before-edit Guard

This release adds a run-scoped guard for existing files: the Writer must
successfully `read_file` a file in the current agent run before a replacement
edit may change that existing file. Full `content` writes are only allowed for
new-file creation, so a paginated read cannot unlock whole-file replacement.
Files created or changed in the same run become known for follow-up replacement
edits. `grep`,
`find_references`, Project Map, and Symbol overview remain navigation hints, not
edit permission.

Targeted tests cover blind existing-file edits, failed reads, new-file creation,
same-run follow-up edits, `read_files`, `parallel(read_file)`, path
normalization, baseline capture, and rejection of whole-file `content` writes
after a paginated read. A fake-provider flow verifies the model can recover
after the guard blocks a blind replacement by reading the file and retrying.

Live Edge/CDP A/B across DeepSeek, Qwen, MiMo, and GLM ran three small project
tasks each with the guard disabled and enabled. The four web models generally
read files before editing, so the guard did not need to block any live edit. The
important live signal is that the guard did not add visible burden or regress
success on compliant providers: DeepSeek, Qwen, and MiMo passed all 6/6
baseline/guard arms; GLM passed 4/6 arms, with its failure unrelated to the
guard because no read-before-edit block fired. The GLM failure exposed smart
quotes in Python edit JSON. A diagnostic route-case rerun passed both arms after
an experimental snippet normalization, but that normalization was removed
before release because it could alter legitimate string content. A later full
GLM rerun was stopped when the site remained rate-limited, so neither run is
reported as a current-build full-matrix pass.

DeepSeek provider reliability also improved: when the page shows the visible
yellow rate-limit retry button with "消息发送过于频繁", Codey waits a short
cooldown, clicks the retry button, and continues waiting for the answer.
GLM applies the same bounded recovery when it shows "请求过于频繁，请稍后再试"
with a visible "重新回答" action. Both providers may retry repeatedly after
cooldowns, bounded by the original request timeout.

GLM smart-quote normalization first accepts already-valid JSON unchanged except
for compile-gated full Python `content`. Structural quote repair runs only after
the original JSON fails to parse, and its candidate must parse successfully
before use. It preserves `old_string`, `new_string`, summary text, and
replacement items exactly, including legitimate typographic punctuation.

The initial project prompt now gives one stable relative-path workspace rule
instead of exposing an absolute temporary project path. The entire Project
instructions section is omitted when neither `AGENTS.md` nor `CLAUDE.md` exists;
when present, those files are still loaded and bounded as before. Unit tests
capture both prompt forms.

A lightweight live guard smoke used one `similar-config-constant` task per web
provider after this prompt change. DeepSeek, Qwen, MiMo, and GLM each passed in
4 turns, changed only `limits.py`, and passed the two local checks. Tool calls
were 3 / 3 / 4 / 3 respectively. No guard block, navigation error, duplicate
submission, rate-limit recovery, or protocol repair occurred.

Validation: `python -B -m unittest` passed with 613 tests; `python -B -m pytest
-q` passed with 621 tests and 31 subtests. Full-tree Ruff, `compileall`,
manual `read_before_edit_ab.py --self-test`, and `git diff --check` passed.

## 0.1.32 Bounded Symbol Overview

This release adds a bounded task-aware Symbol overview as a small section of the
existing Project Map. It gives the Writer file and symbol navigation hints
before the first read, while keeping the old Project Map role for manifests,
docs, roots, candidate commands, and observed checks. It adds no UI, public
tool, persisted index, cache, embedding, LSP integration, or source bodies.

The live Edge/CDP A/B benchmark asks each provider to choose the first files it
would inspect for five Codey maintenance tasks. The final production
`render_project_map(..., task=...)` path improved first-file selection across
all four web providers:

| Arm | Expected-path hits | Top-1 hits |
|---|---:|---:|
| Initial listing only | 18 | 7 / 20 |
| Project Map | 23 | 8 / 20 |
| Project Map + Symbol overview | 40 | 20 / 20 |

Provider-level Symbol overview top-1 result: DeepSeek 5/5, Qwen 5/5, MiMo 5/5,
GLM 5/5. The manual benchmark now lives at
`tests/manual/project_map_symbol_ab.py` and tests the production Project Map
renderer directly.

Qwen reliability also improved: `new_chat()` now tolerates Qwen redirect
`net::ERR_ABORTED` before readiness checks, and `chat()` retries once when a
confirmed submission stalls without producing a response. Other navigation and
timeout failures remain visible.

Validation: `python -B -m unittest` passed with 590 tests; `python -B -m pytest
-q` passed with 598 tests and 31 subtests; changed-file Ruff, `compileall`,
manual Symbol overview self-test, and `git diff --check` passed.

## 0.1.31 Structured Execution Evidence

This release adds a bounded, in-memory evidence ledger for one TaskRunner run.
It records edit epochs, changed files, read ranges, complete and truncated
searches, truncated tool results, duplicate information calls, and successful
or failed checks after the latest edit. Verification Map, Review, receipts, and
successful ProjectFacts now consume the same current check facts. The ledger is
not persisted, adds no UI or public tool, and does not intervene in Writer
convergence.

Live Edge/CDP reference-aware edits passed on all four providers:

| Provider | Result |
|---|---|
| MiMo | passed in 6 turns |
| Qwen | passed in 7 turns |
| GLM | passed in 8 turns |
| DeepSeek | passed in 9 turns |
| MiMo Writer -> Qwen Reviewer | approved; independent unittest passed |

The live run exposed a GLM duplicate-submission false positive. GLM can append
different question nodes for tool results and protocol repair; duplicate
detection now compares the normalized full prompt body for this submission
instead of treating total question-node growth as duplication.

Validation: `582 passed`, `31 subtests passed`, Ruff, `compileall`, and
`git diff --check` all pass.

## Post-0.1.29 Four-Provider Capability Audit

A repeated live Edge/CDP audit evaluated the distinct capabilities introduced
from `0.1.25` through `0.1.29` instead of treating one read-only task as proof
for every release. DeepSeek, GLM, Qwen, and MiMo were exercised on bounded
navigation, reference-aware modification, interrupted-task recovery, and
Verification Map review. The detailed methodology and results are recorded in
`tests/manual/POST_0_1_29_CAPABILITY_AUDIT.md`.

The audit exposed and fixed three concrete reliability problems: literal grep
silently skipped source files larger than 512 KiB, GLM smart quotes could
corrupt Python source embedded in an edit command, and the manual benchmark
could fail while printing Unicode through a GBK Windows console. Search now has
an explicit 8 MiB per-file and 16 MiB total read budget, GLM Python content is
repaired only when the original fails compilation and the quote-normalized
candidate compiles, and manual benchmark output is UTF-8.

Current validation: `575 passed`, `31 subtests passed`, changed-file Ruff,
`compileall`, and `git diff --check`.

## 0.1.30 Outline Tool Withdrawal

The `outline_file` capability introduced briefly in 0.1.26 is now removed from
the public contract, Agent dispatch, hidden audit, runtime, parser, and tests.
Natural tasks consistently selected literal grep or lexical references followed
by offset reads. There is no compatibility alias. The supported navigation chain
is Project Map, grep or find references, then `read_file(offset=...)`.

Validation: `575 passed`, `31 subtests passed`, Ruff, `compileall`, and
`git diff --check` all pass.

Date: 2026-07-11
Environment: Windows / Edge or Chrome CDP reuse path / DeepSeek, MiMo, Qwen, GLM tabs open

## 0.1.29 Verification Map

This release adds bounded, evidence-labeled verification candidates to Review.
It does not claim affected tests, dependency impact, or coverage.

Behavior:

- changed tests are listed directly;
- Python and JS/TS tests can be candidates through strong filename relations;
- Python direct imports use `ast`; relative JS/TS imports use bounded lexical parsing;
- only a small set of declarations added by the diff may produce reference hints;
- successful checks are taken only from after the latest real edit;
- manifest and previously successful project checks are separate broader candidates;
- one bounded traversal scans test files instead of rescanning per symbol;
- excluded directories, symlinks, non-UTF-8/binary, secret-like, and oversized
  files are skipped;
- scan, candidate, result, changed-file, check, and command budgets are explicit;
- attempted test content is capped at 16MB in addition to the 512KB per-file
  limit; non-UTF-8 files consume budget before decoding and cannot bypass it;
- a truncated diff or scan produces an explicit incomplete-discovery warning;
- no candidates renders `(none found)` without suggesting that testing is unnecessary;
- Reviewer is told that candidates are not coverage proof and candidate paths do
  not relax the changed-file-only finding contract;
- construction failure degrades to normal Review with an empty map;
- a later edit or failed verification run clears prior green checks in both
  memory and the durable checkpoint, keeping Review, recovery receipts, and
  successful ProjectFacts semantics aligned.

Live Edge/CDP verification used DeepSeek as Writer and GLM, Qwen, and MiMo as
Reviewers. The map identified `test_app.py` through naming evidence and reported
only the post-edit `python -m unittest`. Qwen initially misread an unchanged
candidate as a missing file; the map now states that candidates are existing
readable local files and absence from Changed files means unchanged. After that
root fix, all three reviewers approved the independently verified change without
a noisy follow-up, and checkpoint recovery remained successful.

Regression result: `573 passed, 33 subtests passed`; changed-file Ruff,
`compileall`, and `git diff --check` passed.

## 0.1.28 Durable Execution Checkpoint

This release adds a bounded, atomic, session-scoped checkpoint for unfinished
project execution. It stores local execution facts only: relative changed
paths with post-edit hashes, successful checks after the latest edit, a small
last-action record, status, and stop reason. It stores no source, diff, tool
output, prompt, task plan, or model-authored remaining work.

Covered behavior:

- successful edits invalidate all earlier checks even if a path/hash cannot be
  recorded; runtime-accepted relative or in-project absolute paths are
  canonicalized under the project root before hashing;
- successful runs are recorded, while failed runs remain only the last action;
- recovery reconciles hashes and invalidates checks after external changes;
- stopped, errored, max-turn, and no-progress tasks retain an interrupted checkpoint;
- only the same session/project with an explicit continuation, or the same task
  after provider-context loss, receives the recovered facts;
- unrelated new tasks do not inherit an old checkpoint;
- recovered changes still enter final Diff/Review even if the resumed Writer
  performs no additional edit;
- a failed verified ProjectFacts write does not delete the checkpoint early;
- normal completion removes the active checkpoint;
- corrupt, oversized, or incompatible checkpoint files are ignored safely.

Live Edge/CDP verification intentionally stopped DeepSeek at `max_turns=1`
immediately after creating `app.py`, closed the provider, opened a fresh
DeepSeek conversation with the recovered checkpoint, read the real files, ran
`python -m unittest`, completed successfully, passed an independent local
check, received GLM approval, and deleted the completed checkpoint.

Regression result: `555 passed, 33 subtests passed` with pytest; changed-file
Ruff, `compileall`, and `git diff --check` also passed.

## 0.1.27 Find References Tool

This release adds `find_references`, a bounded lexical reference-hints tool for
inspecting likely impact sites before changing a symbol. It does not add UI,
cache, indexing, LSP, semantic resolution, persistence, or a reviewer tool.

Behavior:

- `find_references` is exposed in the JSON tool contract and runs as local
  runtime tool `references`.
- The tool accepts only simple symbols such as `createRouter`,
  `SessionStore`, `login_user`, or `$state`; complex expressions should use
  `grep`.
- Results are explicitly labeled as lexical hints only, not semantic resolution
  or a complete call graph.
- Output is stable, path/line based, and capped at 80 matches with precise
  truncation metadata.
- The scanner skips dependency/build/cache directories, large files, unreadable
  files, non-UTF-8 files, and symlinked paths.
- Direct dependency/build/cache start directories are skipped case-insensitively
  instead of being scanned as a special case.
- A selected project root named like an excluded directory, such as `build`,
  `dist`, or `target`, still scans normally.
- Direct symlink start paths are rejected before resolving, so a project-local
  link cannot silently redirect the scan to its target.
- `find_references`, normal `grep`, and hidden project-audit search/reference
  scans share the same streaming bounded scanner; none of them collects and
  sorts every candidate file before starting work.
- When the shared scanner reaches a file, directory, or per-directory entry
  budget, the tool result explicitly says omitted files may contain more
  matches.
- `find_references` is not `parallel_safe`; `parallel` still accepts only
  `list_dir`, `read_file`, and `grep`.
- Hidden project-audit advisors may use `find_references`, but only through the
  same sensitive path, symlink, binary, excluded-directory, and size checks used
  by audit reads.
- Reviewer still receives diff/log/brief/map context only; no reviewer-side tool
  access was added.

Verification:

| Flow | Result |
|---|---|
| Runtime / protocol / agent / consensus / live-smoke focused suite | 160 passed |
| Full pytest suite | 542 passed |
| Syntax compile | `python -m compileall -q codey tests tools` passed |
| Diff whitespace check | `git diff --check` passed with no output |
| DeepSeek live `find_references` smoke | passed in 8 turns |
| GLM live `find_references` smoke | passed in 7 turns; unique indentation recovery exercised |
| Qwen live `find_references` smoke | passed in 7 turns |
| MiMo live `find_references` smoke | passed in 8 turns; edit path protocol correction exercised |

## 0.1.26 Outline File Tool (withdrawn)

`outline_file` was removed after the natural-use audit. Controlled prompts could
make providers use it, but normal tasks consistently preferred literal `grep`
followed by offset `read_file`. Removing the tool also removes its public/runtime
contract, hidden-audit branch, Python/JS/TS outline parser, and maintenance tests.
There is no compatibility alias. The supported navigation chain is now Project
Map, literal grep or lexical references, then offset source reads.

Verification:

| Flow | Result |
|---|---|
| Tool runtime / protocol / agent / consensus focused suite | 113 passed |
| Full unittest suite | 510 passed |
| Full pytest suite | 510 passed |
| Syntax compile | `python -m compileall -q codey tests tools` passed |
| Diff whitespace check | `git diff --check` passed with no output |

## 0.1.25 Hidden Project Map

This release adds a deterministic, bounded Project Map as a hidden first-turn
orientation layer. It does not add UI, storage, indexing, RAG, or another model
call.

Behavior:

- Before project tasks, Codey builds one bounded local Project Map and passes it
  to the Writer.
- The same Project Map is shared with hidden consensus/project-audit advisors so
  advisors start from the same safe local structure.
- Review receives a refreshed Project Map after Writer changes are collected, so
  newly added files such as tests or manifests are visible to the Reviewer.
- Review path rules remain strict: Project Map is only context for coverage and
  integration judgment; findings still must point to changed files.
- The map includes only relative-path facts: source/test roots, manifests, docs,
  key directories/files, observed successful checks, and candidate commands.
- Candidate commands are explicitly marked as candidates, and nested manifests
  carry their working directory, for example `web/: npm test`.
- The scanner does not read source contents, does not follow symlinks, skips
  dotfiles, secret-like files, env/key/certificate files, lock files,
  dependency/build/cache directories, and uses a strict directory-entry budget.

Verification:

| Flow | Result |
|---|---|
| Project Map unit tests | 8 passed |
| Project Map / Writer / consensus / Review / server focused suite | 160 passed |
| Full unittest suite | 501 passed |
| Full pytest suite | 501 passed |
| Syntax compile | `python -m compileall -q codey tests tools` passed |
| Diff whitespace check | `git diff --check` passed with CRLF warnings only |

## 0.1.24 Hidden ChangeBrief and Plan-Aware Review

This release borrows the smallest useful part of lightweight spec workflows:
existing exploration output is converted into a hidden, bounded task brief.
No visible spec UI, project artifact, storage migration, command syntax, or
confirmation gate was added.

Behavior:

- Empty-project hidden planning now becomes a private `ChangeBrief` before the
  Writer starts.
- Existing-project read-only audit reports now become the same private
  `ChangeBrief` format.
- The Writer receives the `ChangeBrief` instead of loose advisory prose.
- The Reviewer receives the same `ChangeBrief` and checks intent satisfaction,
  acceptance checks, non-goals, and risks in addition to diff correctness.
- Simple direct write tasks that do not have hidden planning or audit context do
  not receive a `ChangeBrief`.
- Successful project facts now include recent verified changes, but only after
  a task finishes with real file changes and a passing local check.
- Successful-change facts store only task excerpt, changed files, successful
  check commands, and the task receipt. Model-authored behavior summaries are
  not persisted.
- Existing `facts.json` command-only state remains valid; a missing
  `successful_changes` field is treated as empty.

Verification:

| Flow | Result |
|---|---|
| ChangeBrief/review/project-facts/server focused suite | 97 passed |
| MoA snake flow script unit tests | 6 passed |
| Full unittest suite | 491 passed |
| UI browser E2E | Included in full unittest; hidden project consensus updated for `ChangeBrief` |
| Syntax compile | `python -m compileall -q codey tests tools` passed |
| Diff whitespace check | `git diff --check` passed with CRLF warnings only |

Live MoA snake flow:

```powershell
python -B tests\moa_snake_flow.py --project E:\snake --reset --json
```

Result:

- Passed on 2026-07-10 with DeepSeek as Writer and GLM, MiMo, and Qwen as
  advisors/reviewers.
- The flow covered New Chat MoA discussion, empty-project implementation,
  independent local verification, explicit diff review across GLM/MiMo/Qwen,
  explicit read-only project audit across GLM/MiMo/Qwen, Writer follow-up, and
  final independent verification.
- The generated `E:\snake` project passed `python -B -m unittest` with 12 tests.
- Smoke artifacts are written under the target project at
  `E:\snake\.codey\smoke\moa-snake-flow`, not under the Codey repository.
- The second full run reported no provider errors and no single provider send
  above the 120-second slow threshold. The first probe exposed one transient
  Qwen textarea click timeout during a borrowed-advisor follow-up; it did not
  reproduce in the clean rerun, so no provider-specific workaround was added.

## 0.1.23 Browser Launch Robustness and Explicit Truncation Guidance

This release keeps the UI unchanged while making browser startup failures
actionable and local truncation visible to the model.

Behavior:

- Browser auto-launch honors `CODEY_BROWSER_PATH` when it points to an existing
  executable.
- If no explicit path is configured, Codey still prefers Edge default install
  paths and falls back to system or per-user Chrome install paths.
- Auto-launched Edge and Chrome use separate profiles:
  `~/.codey/edge-profile` and `~/.codey/chrome-profile`.
- Missing browser startup now reports a clear Edge/Chrome/CODEY_BROWSER_PATH
  error.
- If `webview.start()` fails, Codey prints the local URL, explains that the
  native window could not open, and keeps the HTTP server available until
  Ctrl+C.
- `ToolResult` now carries `truncated`.
- Model-facing tool results mark truncated output with `truncated=true` and
  explicitly say omitted content may still contain relevant errors or code.
- Approved Shell continuation also tells the writer when Shell output was
  truncated.
- Review prompts warn when the diff was truncated and instruct the reviewer not
  to assume omitted hunks are clean.
- No read/run/diff size limits were increased, no full output replay was added,
  and no UI was added.

Verification:

| Flow | Result |
|---|---|
| Browser/protocol/review/server focused suite | 130 passed |
| Full unittest suite | 478 passed |
| Syntax compile | `python -m compileall -q codey tests tools` passed |
| Diff whitespace check | `git diff --check` passed with CRLF warnings only |
| Browser fallback | `CODEY_BROWSER_PATH`; Edge first; system/per-user Chrome fallback; separate profiles |
| WebView fallback | HTTP server remains available with manual URL after native-window failure |
| Truncation guidance | `truncated=true` tool results and diff truncation review note |

## 0.1.22 Durable Conversation Handoff

This release connects durable visible chat history to the existing compact
conversation handoff. The goal is to reduce model "amnesia" after a Codey
restart or model switch without replaying the full transcript or adding UI.

Behavior:

- `UiStateStore` can extract a bounded visible excerpt for one saved session.
- The excerpt includes only recent user messages, assistant replies, Review
  receipts, Done receipts, and compact Changes receipts.
- Tool events, turn markers, Shell output, teaching cards, and other local
  execution noise are skipped.
- The current user request is skipped so continuation prompts do not duplicate
  the active message.
- Fresh web-model chats after restart, model switch, or provider-session loss
  merge the compact factual snapshot with the bounded visible excerpt.
- The selected model receives the recovered handoff and can continue the
  conversation naturally.
- Hidden MoA advisors still receive only the compact factual snapshot; the
  recovered visible chat excerpt is not automatically spread to other web
  models.
- If no visible excerpt exists, normal MoA chat keeps using the previous compact
  context path and does not create an owner-only recovered prompt.
- No full transcript replay, export button, training pipeline, or new UI mode
  was added.

Verification:

| Flow | Result |
|---|---|
| Focused recovered-handoff suite | 72 passed |
| Full unittest suite | 469 passed |
| Syntax compile | `python -m compileall -q codey tests tools` passed |
| Diff whitespace check | `git diff --check` passed with CRLF warnings only |
| Recovered owner prompt | Selected model gets compact snapshot plus bounded visible excerpt |
| Hidden advisor context | MoA advisors keep compact factual snapshot only |
| Current request duplication | Current user request is skipped from recovered visible excerpt |
| Export/training UI | None |

## 0.1.21 Durable Chat State and Quiet Controls

This release makes visible chat state durable while keeping the UI small and
local-only. It also keeps the quiet per-message copy control and the shared
Send/Stop action slot.

Behavior:

- User messages, assistant replies, Review receipts, and Done receipts expose a
  per-message copy button.
- Copy uses the browser Clipboard API with an `execCommand("copy")` fallback.
- The copy control stays subdued by default and becomes clear on hover or
  keyboard focus.
- Send and Stop now share one composer action slot.
- Idle state shows `Enter` plus the send arrow.
- Running state shows `Stop` plus the square stop icon.
- `updateSend()` is the single path that switches send, stop, and hint state.
- The native WebView runs with `private_mode=False` and persistent storage at
  `~/.codey/webview`.
- Chat sessions, titles, messages, terminal receipts, active chat, and sidebar
  projects are stored as one bounded backend snapshot at
  `~/.codey/ui-state.json`.
- Browser `localStorage` remains a fast cache; `/api/ui_state` is the durable
  recovery source.
- UI state recovery runs before opening the SSE event stream, so reconnect or
  task events are not dropped while restored sessions are still loading.
- UI state writes use `(updated_at, revision)` ordering so same-millisecond
  async saves cannot overwrite newer state.
- The backend snapshot keeps only the UI schema fields Codey renders, with
  bounded sessions, projects, messages, terminal runs, and string lengths.
- No export button, training pipeline, or chat database workflow was added.

Verification:

| Flow | Result |
|---|---|
| Focused UI-state suite | 43 passed |
| Full unittest suite | 464 passed |
| Syntax compile | `python -m compileall -q codey tests tools` passed |
| Diff whitespace check | `git diff --check` passed with CRLF warnings only |
| WebView persistence contract | `private_mode=False`; `storage_path=~/.codey/webview` |
| Backend UI snapshot | `~/.codey/ui-state.json` |
| Export/training UI | None |

## 0.1.19 MiMo Answer-State Completion

This release narrows MiMo's state split: send-button recognition can still use
the MiMo paper-plane SVG as a provider-specific final guard before clicking,
but answer completion no longer depends on the send icon returning.

Behavior:

- `_generation_complete()` now locates the newest answer and evaluates answer
  DOM state instead of composer icon state.
- Cleaned final text must exist after removing MiMo thinking/deep-thinking
  blocks.
- `data-is-typing="true"` rejects completion.
- A copy button near the newest answer marks the answer complete.
- If no copy button is available yet, completion requires that MiMo is not in a
  generating/stop state.
- SVG matching remains only in the send-button path to avoid upload/stop
  misclicks.

Verification:

| Flow | Result |
|---|---|
| MiMo unit suite | 29 passed |
| Focused provider/protocol/server suite | 211 passed |
| Full unittest suite | 452 passed |
| Syntax compile | `python -m compileall -q codey tests tools` passed |
| Diff whitespace check | `git diff --check` passed with CRLF warnings only |
| New UI modes or controls | None |

## 0.1.18 Provider Reliability and Factual Receipts

This release keeps the visible UI unchanged and tightens the provider and
review boundaries underneath it.

Changes:

- MiMo accepts only its explicit paper-plane send button and rejects nearby
  upload controls.
- MiMo treats the page as busy while the stop/generating state is active and
  refuses to submit another message during that state.
- MiMo removes thinking/deep-thinking blocks before reading the final answer
  and does not fall back to visible answer text while generation is active.
- Qwen uses a strict local `button.send-button` fallback before teaching, and
  hidden Review runs suppress manual teaching and ProfileDoctor assistance.
- GLM can detect a replaced answer when the response count does not increase,
  and GLM-only smart-quote normalization also covers review JSON.
- The shared JSON protocol rejects nested tool calls hidden inside
  `done(summary)` and directs models to call the tool directly.
- Legacy write-style tool names are no longer accepted; models must use
  `edit(content=...)` for creating or replacing a file.
- Review repair follow-up tracks whether the repair turn actually ran checks,
  so failed checks or unfinished repair turns no longer inherit a previous
  `checks passed` receipt.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 450 passed |
| Focused provider/protocol/server suite | 209 passed |
| Agent and server regression suite | 116 passed |
| Syntax compile | `python -m compileall -q codey tests tools` passed |
| Diff whitespace check | `git diff --check` passed with CRLF warnings only |
| MiMo unit contract | 27 tests passed; send button, upload rejection, stop-state rejection, thinking cleanup, and generation gating covered |
| Qwen unit contract | Strict local send fallback covered before teaching |
| GLM unit contract | Smart-quote review JSON and replaced-answer detection covered |
| Protocol contract | Nested tool calls inside `done(summary)` rejected; write-style tools rejected |
| Review receipt regression | Failed repair checks and no-progress repair turns do not inherit `checks passed` |
| Real MiMo single-send smoke | Final answer returned; no upload popover and no stopped-response state |
| Real DeepSeek + MiMo project audit smoke | MiMo returned a valid hidden audit report |
| Real DeepSeek write + MiMo review smoke | Review completed with `ok: true` and `approved` |
| New UI modes or controls | None |

## 0.1.17 Hidden MoA Layer

MoA is a hidden consultation layer, not a UI mode. When already-open
provider pages are available, `New Chat` asks the selected model for a private
draft, lets up to two other models critique or supplement it independently,
then asks the selected model to produce one final answer. Empty or
placeholder-only projects use the same owner-first pattern for one hidden
advisory plan before the Writer starts. Existing projects keep bounded
read-only advisor audits before the selected model acts; private audit reports
are passed to the selected Writer as advisory input, and the Writer still
verifies against real files.

Boundaries:

- Chat advisors cannot use tools, edit files, run commands, browse, or see the full project.
- Chat and empty-project advisors review the selected model's private draft; they do not see each other's notes.
- Project audit advisors can only list, grep, and read selected project files.
- Dotfiles, env files, secret-like paths, excluded dependency/build directories, key/certificate files, lock files, binaries, symlinks, and oversized files are not shared with hidden project audit advisors.
- Project audit advisors cannot edit, run commands, request shell approval, or access paths outside the project.
- While a Writer tab is active, hidden advisors are borrowed from already-open sibling tabs instead of opening another CDP connection.
- Each project audit advisor has a bounded total time budget; unfinished advisors produce no private report.
- If no advisor is available, the task falls back to the normal single-model path without generating a private draft.
- If draft-first advisors fail, the selected model's draft is used as the quiet fallback.
- If final synthesis fails after a private draft, Codey does not resend the original prompt.
- New Chat emits one ordinary assistant reply.
- Empty projects can receive one hidden plan before the selected Writer starts.
- Existing project audits are private reports; the selected Writer still verifies and decides.
- Project tasks that finish with `changed=False` can still refine the final read-only answer.
- Review remains a separate post-change acceptance layer over the final Diff.
- The tool protocol tells the model not to claim command, test, build, lint, or shell results unless they came from a local `run` or `shell` tool result.
- The web UI adds no buttons, modes, model-vote display, or group chat surface.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 424 passed |
| Consensus unit contract | Automatic advisor selection, owner draft, independent critiques, bounded prompts, failures degrade |
| Owner draft prompt with long handoff | Current user request remains a separate field and is not clipped away by handoff context |
| New Chat follow-up consensus context | Handoff is passed once as context; owner prompt stays empty to avoid duplicate handoff text |
| Server New Chat consensus | Agent loop not called; one `reply` emitted |
| New Chat draft-first consensus | Selected model draft is sent before advisors; degraded draft result forgets the provider session |
| New Chat synthesis failure | Original prompt is not resent after an unexpected hidden synthesis failure |
| Project audit advisor tools | Read-only file inspection works; attempted writes are rejected and files remain unchanged |
| Project audit secret boundary | `.env`, `prod.env`, credential files, excluded directories, symlinks, and secret search hits are not sent to hidden advisors |
| Project audit unfinished advisor | No `done(summary)` means no report is passed to the Writer |
| Sibling-tab advisor connection | Hidden consensus and project audit borrow already-open tabs from the active Writer context |
| GLM split markdown response | One assistant answer split across multiple `.markdown-body` nodes is read as one complete answer wrapper |
| GLM smart-quote tool JSON | GLM-only normalization repairs smart double quotes around tool JSON without changing the shared JSON codec |
| Qwen review path contract | Live Qwen review probe returned `game.js`, copied from Changed files, instead of inventing a new test filename |
| Existing project audit | Private reports are injected into the selected Writer task |
| Existing project audit failure | Writer continues without the private reports |
| Empty project plan | Draft-first hidden plan is injected before Writer starts in a fresh chat |
| Project read-only consensus | Final answer can be refined after a no-change project task; no Review |
| Project read-only synthesis failure | Writer answer is kept and the provider session is forgotten |
| Project write task | Writer edits; Review still runs after Diff |
| Real Edge UI E2E | All 21 checks passed, including hidden consensus and existing restore/reconnect paths |
| Live Edge MoA project review | Passed with DeepSeek Writer plus MiMo and Qwen hidden audit advisors; two reports collected, no `.env` marker leaked, no files changed |
| Live Edge snake stress loop | Root cause traced to GLM reading only the last `.markdown-body` fragment of a split answer plus occasional smart quotes; wrapper-based GLM response reading and GLM-scoped smart-quote cleanup were added |
| New UI modes or controls | None |

## 0.1.16 Project and Plain Conversations (2026-07-06)

`New Chat` remains a normal model conversation with no project path and never
enters the local Agent tool loop. A project conversation now supports both
read-only discussion and coding without a mode switch: the selected model may
inspect the project and answer directly, but edits only when requested. The
second model is used for Review only when the current Agent run actually wrote
a file.

The project answer and optional change receipt are two views of the same
`task_done` event. This keeps reconnect recovery atomic and deduplicated while
hiding the unhelpful `No files changed` receipt for discussion-only turns.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 388 passed |
| Global New Chat route | Direct Provider reply; Agent tool loop not called |
| Project read-only discussion | Multiline answer preserved; no writes and no Review |
| Project edit task | Final answer shown before tested change receipt and Diff |
| Real Edge UI E2E | All 19 checks passed, including plain chat, project discussion, and reconnect recovery |
| Live DeepSeek project discussion | Passed in 1 turn; no files created or changed |
| New UI modes or controls | None |

## 0.1.15 GLM Provider

GLM is registered as a fourth provider and reuses the existing browser,
one-shot submission, cancellation, recovery, diagnostics, and task-context
boundaries. Its local profile anchors the composer, accepts the send control
only after non-whitespace text enables it, and selects the final Markdown
answer while excluding the neighboring thinking panel. A GLM-only formatting
note puts JSON in a code block with ASCII quotes; normal chat remains normal.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 384 passed |
| Provider registry, profile, browser wrapper, CLI, and smoke choices | GLM included and cross-checked |
| Blank or whitespace-only composer state | Send remained unavailable |
| Blank `GlmWebProvider.send()` input | Rejected before formatting or page access |
| Non-empty composer state | Send became available and was verified before click |
| Thinking and final answer DOM | Final answer selected independently |
| Discovery, Doctor, teaching, or cached response inside/around thinking DOM | Rejected before text is read |
| One-shot uncertain submission | No second click; delayed answer still observed |
| Duplicate question guard | Extra user question is reported rather than silently accepted |
| Full 4K tool protocol on live GLM page | Returned valid ASCII JSON; one question after completion |
| Live GLM edit task, first run | Passed in 4 turns; independent unittest passed |
| Live GLM edit task in four-provider matrix | Passed in 4 turns; independent unittest passed |
| Live GLM review of a DeepSeek edit | Approved; independent unittest passed |
| Live GLM edit after final-answer guard | Passed in 5 turns; independent unittest passed |
| Real Edge UI E2E | All 16 checks passed, including GLM picker selection |
| Existing DeepSeek and MiMo matrix tasks | Passed in 4 / 4 turns |
| Qwen startup root cause | Composer appeared before `/api/v2/models/` completed; click reached the React handler but emitted no chat request |
| Qwen startup repair | Waits for successful model bootstrap; necessary draft settling and A/B choice handling remain |
| Fresh Qwen edit reruns | Passed in 4 / 5 turns; both independent unittest checks passed |
| New UI concepts | None; one provider row added |

## 0.1.14 Protocol Efficiency and Safety

The JSON tool protocol now derives its public names, aliases, examples,
read-only properties, and result labels from one compact contract. Unsafe
`parallel` batches are rejected as a whole before execution. `read_file`
returns large UTF-8 files in complete-line pages with bounded line and file
content character counts, while preserving the exact output of small and empty files.
Structured `replacements` apply up to eight changes to one file after every
search is validated in memory; a failed later search leaves the file intact.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 363 passed |
| Tool contract examples and aliases | Generated and parsed from one immutable specification |
| Unsafe `parallel` with a valid read before an edit | Entire batch rejected; no tool event or file change |
| `parallel` wrappers, commands, writes, nesting, and over-limit batches | Rejected before execution |
| `read_file` pagination | Complete lines, explicit next offset, 300-line default, 600-line maximum, 16,000-character file-content budget |
| Empty and small files | Previous output preserved exactly |
| Overlong single line | Bounded head/tail preview marked unsafe for `old_string` |
| Atomic replacements | Later validation failure leaves original bytes unchanged; eight-item limit enforced in codec and runtime |
| Real Edge UI E2E | All 16 checks passed |
| Live DeepSeek edit task | Passed in 4 turns; unittest passed |
| Live Qwen edit task | Passed in 4 turns; `read_files` used; unittest passed |
| Live MiMo edit task | Passed in 4 turns; unittest passed |
| New UI controls or cards | None |

## 0.1.13 Runtime Ownership and Provider Context

Git and snapshot change collection now share `changes.py`, while the HTTP
server retains only transport and request orchestration. Edge profile, CDP
state, learned controls, continuity data, and recovery snapshots use one local
state root. UI, CLI, and smoke tasks explicitly own provider-local context and
clear it after every exit path, including connection and CDP close failures.
Qwen keeps one real trailing keyboard input so its controlled composer commits
the same text that Codey verifies and submits.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 344 passed |
| Git and snapshot change ownership | Unified in `changes.py`; server transport unchanged |
| Local runtime paths | Edge profile, CDP state, controls, facts, conversations, and snapshots share one root |
| Provider task-local context | Cleared after success, cancellation, task failure, connection failure, and CDP close failure |
| CLI and smoke close-failure regression tests | All four entry points passed |
| Removed runtime residue | Unused `State.events` queue removed |
| Real Edge UI E2E | All 16 checks passed |
| Live DeepSeek edit task | Passed in 5 turns; unittest passed |
| Live Qwen edit task | Passed in 9 turns; all submissions confirmed and unittest passed |
| Live MiMo edit task | Passed in 4 turns; unittest passed |
| New UI controls or cards | None |

## 0.1.12 Run Reconciliation and One-Shot Submission

The backend now reserves a unique run ID before browser work is queued and
keeps one bounded in-memory snapshot of the active run, pending approval or
teaching request, and last terminal event. After an SSE reconnect, the UI
reconciles against that snapshot, restores the existing controls, and dedupes
the terminal receipt by run ID. Short interruptions stay silent; only a
connection that remains unavailable for five seconds reuses the existing
status line for `Reconnecting…`.

DeepSeek, Qwen, and MiMo now share one submission boundary. Each provider
chooses click or Enter before the remote action, marks the attempt first, and
performs no second action after an exception or unconfirmed result. An
uncertain attempt continues through the complete original response window and
is accepted if the answer appears later. Qwen input uses its real keyboard
path because live testing showed that direct DOM filling changed the visible
textarea without updating the website's send state.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 331 passed |
| Atomic run reservation and snapshot isolation | Passed |
| Approval and teaching recovery after reconnect | Passed |
| Chat clear revokes the matching saved terminal receipt | Passed |
| Terminal receipt deduplication by run ID | Passed |
| Delayed, quiet reconnect status | Passed |
| Delayed answers after uncertain submission on all three providers | Passed |
| Shell result from HTTP while SSE is closed | Passed and deduplicated |
| Shell result snapshot after both HTTP and SSE are lost | Restored once; approval card removed |
| Disconnected Allow followed by completed continuation | Executed appears before Done |
| Stale busy snapshot returned after newer task_done SSE | Buffered event replay leaves UI done |
| Real Edge UI E2E | All 16 checks passed, including five reload/reconcile flows |
| Live DeepSeek edit task | Passed in 5 turns; unittest passed |
| Live Qwen edit task | Passed in 4 turns; unittest passed |
| Live MiMo edit task | Passed in 4 turns; unittest passed |
| New UI controls or cards | None |

## 0.1.11 Responsive Stop and Bounded Output

One task-local cancellation signal now covers provider polling, browser and
control recovery waits, ProfileDoctor, review, and controlled `run` processes.
Stopping does not click a provider website's stop-generation control. It
discards the provider session, reports `task_done: stopped`, and makes the next
task open a fresh conversation. Controlled subprocesses are terminated with
their process trees. Long `run` and approved Shell output share one pure
head-and-tail clipping function and do not retain a hidden full log.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 310 passed |
| Cancellation and real Windows parent/child process-tree tests | Passed |
| Provider, ProfileDoctor, Review, and TaskRunner cancellation tests | Passed |
| Head-and-tail output tests for `run` and approved Shell | Passed |
| Real Edge UI E2E | All 10 checks passed, including responsive Stop |
| Live DeepSeek cancellation | Cancelled; fresh conversation opened |
| Live Qwen cancellation | Cancelled in under 1 ms measured latency; fresh conversation opened |
| Live MiMo cancellation | Cancelled in under 1 ms measured latency; fresh conversation opened |
| UI surface | Unchanged |

## 0.1.10 ProfileDoctor

ProfileDoctor runs only after deterministic local recovery cannot choose a
provider-page candidate safely. It exposes at most eight candidates, removes
conversation and answer text, reduces labels and DOM identifiers to a fixed
semantic vocabulary, and replaces exact geometry with coarse buckets. One
already-open sibling provider may return only a candidate ID or `null`. The
request is one-shot, assistance is disabled during the call to prevent
recursion, and the existing state validation remains the only path that can
persist a learned control. A failed or invalid decision falls through to the
existing human teaching flow.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 294 passed |
| One-call, no-recursion, strict decision-schema tests | Passed |
| Sanitization and bounded-candidate tests | Passed |
| Malicious tag / role / type values | Reduced to fixed `other` enum values |
| Deterministic → Doctor → human ordering tests | Passed |
| Validation-before-save and storage-failure tests | Passed |
| Same-CDP sibling-tab borrowing tests | Passed |
| Real Edge UI E2E | All 9 checks passed; UI unchanged |
| Forced DeepSeek / Qwen / MiMo send recovery | Doctor selected one candidate; submission verified before save |
| Provider-added response status text | Parsed deterministically without a second model call |

## 0.1.9 Bounded Provider Recovery

The three provider drivers now read their normal selectors from one validated,
versioned local profile. If those selectors stop matching, a bounded discovery
layer scores only composer controls and DOM regions changed after submission.
Actionable learned selectors must resolve to one visible control, and Codey
persists them only after observing successful input, submission, or answer
reading. Two failed validations remove a learned record. Optional learning
storage is atomic and cannot fail an otherwise successful provider operation.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 277 passed |
| Provider-focused tests | 51 passed |
| Real Edge UI E2E | All 9 checks passed; UI unchanged |
| Normal DeepSeek / Qwen / MiMo tasks | Passed in 5 / 4 / 4 turns |
| Forced DeepSeek core-selector failure | Recovered; input, send, and answer records verified |
| Forced Qwen core-selector failure | Returned `RECOVERY_OK`; all three records verified |
| Forced MiMo core-selector failure | Returned `RECOVERY_OK`; all three records verified |
| Optional storage failure | Did not interrupt the successful provider operation |
| Syntax and patch checks | Passed |

The recovery scope remains deliberately small. It does not bypass login or
CAPTCHA challenges, click low-confidence controls, retain page DOM, or add any
new interface concept. Human one-click teaching remains the final fallback.

## 0.1.8 Hidden Local Continuity

Version 0.1.8 added three bounded, invisible continuity mechanisms:

- Project Facts records only successful controlled `run` commands and injects
  them into later tasks for the same project.
- Conversation persistence stores one compact factual snapshot for each recent
  chat, so a restarted Codey process opens a fresh provider chat with a silent
  handoff instead of losing the task state.
- Durable Snapshot atomically stores the non-Git recovery baseline before a
  project write, retains expected after-content hashes for conflict detection,
  and deletes the record after a successful restore.

The three mechanisms share only a small atomic JSON writer. They use separate
files and retention rules, and none adds a UI control, banner, or status message.
Git projects continue to use Git and do not persist a recovery snapshot.

Verification:

| Flow | Result |
|---|---|
| Full unittest suite | 260 passed |
| Abrupt child-process exit | Facts, conversation, diff, and restore survived |
| Snapshot save failure | Project write was blocked |
| Manual edit after Codey write | Restore conflict preserved user content |
| Missing post-write hash after an interrupted write | Restore refused to overwrite current content |
| Chat deletion racing a late save | Deleted context stayed deleted |
| Non-Git project initialized as Git | Old tracker persistence was disabled; stale references could not recreate recovery |
| Real Edge UI E2E | Passed; UI unchanged, diff and restore passed |
| DeepSeek / Qwen / MiMo edit matrix | Passed in 5 / 4 / 4 turns |
| Independent provider verification | All three unittest fixtures passed |

The live provider run also emitted Node's existing `DEP0169` warning from the
browser automation runtime. It did not affect any provider result and is not
introduced by the local continuity code.

## 0.1.7 Maintainability Refactor

The agent now produces one structured `RunEvent` stream. CLI logs, review
context, SSE updates, and the UI are projections of that stream; the UI no
longer parses human log strings. Local tools return structured `ToolOutcome`
values, so check success and exit codes are not inferred from display text.
Task orchestration moved from the HTTP handler module into `task_runner.py`.

No legacy `agent.tool_*` wrappers were retained solely for tests. Tests now
exercise `tool_runtime.py` directly.

Follow-up regressions cover successful empty-file reads, no-op writes and
edits, and the absence of the legacy string `log` SSE path. A successful tool
call now counts as progress only when `ToolOutcome.changed` is true.

Verification after the refactor:

| Flow | Result |
|---|---|
| Full unittest suite | 224 passed |
| Real Edge UI E2E | Pass, including diff, restore, and shell denial |
| DeepSeek / Qwen / MiMo edit matrix | Pass, 5 / 4 / 4 turns |
| DeepSeek / Qwen / MiMo create matrix | Pass, 4 / 4 / 3 turns |
| Three writer/reviewer pairs | All approved |
| DeepSeek / Qwen / MiMo self-bootstrap | Pass, 5 / 5 / 6 turns |
| Same-model chat continuation | Marker preserved |
| DeepSeek to Qwen handoff | Marker and decision preserved |

Every live project smoke used a temporary directory and independent
post-agent verification. Every bootstrap smoke repaired a temporary Codey
copy and then passed all 219 tests.

## 2026-06-30 End-to-End Coverage

The UI now has a repeatable browser E2E test that launches real Edge
against the real local HTTP/SSE server. A deterministic provider drives the
agent so the test can assert the complete product flow without depending on
model variability:

- add a project through the UI
- select a provider
- submit a task and receive SSE updates
- write a file and run its test
- display review and task receipt status
- open and expand the diff drawer
- restore the snapshot and verify the file is removed
- deny a shell approval request and verify no command is executed
- capture screenshots for the completed and restored states

```text
python -B tools/ui_e2e.py --artifacts .e2e-artifacts --json
PASS
```

The live smoke runner now supports a three-provider matrix and independently
verifies the temporary project after the agent finishes. A model returning
`done` is no longer enough to pass: Codey separately executes a functional
assertion and the fixture's unittest suite.

```text
python -B tools/live_smoke.py --provider all --case edit --port 9222 --max-turns 10 --json
DeepSeek: PASS (5 turns)
Qwen: PASS (4 turns)
MiMo: PASS (4 turns)

python -B -m unittest
Ran 224 tests
OK
```

The browser E2E exposed raw JSON protocol payloads and routine `[agent]` logs
in the chat stream. The UI now keeps those internal details out of the chat
and shows only turn dividers, tool rows, review state, and the final receipt.

## 0.1.6 Update

Version `0.1.6` adds a hidden, provider-neutral context handoff:

- one lightweight token estimate for every model
- a soft rollover point near 150k estimated tokens and a 200k hard budget
- one final hidden model turn that returns a bounded factual JSON summary
- a fresh model chat seeded with that summary
- local-fact fallback when summarizing fails
- no budget reset until the fresh chat's first handoff message succeeds
- no new UI controls, context meter, command, or compression notice

Automated verification:

```text
python -B -m unittest
Ran 210 tests
OK
```

Live verification:

| Flow | Result |
|---|---|
| DeepSeek chat summary -> fresh chat | Preserved two decisions |
| Qwen chat summary -> fresh chat | Preserved two decisions |
| MiMo chat summary -> fresh chat | Preserved two decisions |
| Qwen project summary -> fresh chat | Three edits completed; 3 tests passed |
| Qwen empty response recovery | Regenerated once and continued normally |

All project live tests used temporary directories. The main repository was not used as a model editing target.

## Previous 0.1.4 Update

Version `0.1.4` adds compact task receipts in the chat stream:

```text
DONE · 2 files changed · checks passed · restore available        View diff
```

The receipt is built from local facts, not model prose:

- changed file count comes from Git or snapshot changes
- `checks passed` comes from a successful local `run`
- `restore available` appears only for snapshot changes that can be restored
- `View diff` reuses the existing right-side changes drawer

Verification:

```text
python -B -m unittest
Ran 183 tests
OK

python -B -m unittest tests.test_receipt tests.test_agent tests.test_server tests.test_ui
Ran 82 tests
OK
```

Manual UI preview used a temporary local server that exercised the real send/SSE path. The chat showed the receipt line, `View diff` opened the drawer, and expanded diffs showed red/green lines with line numbers.

The 0.1.3 live provider smoke below remains the latest full DeepSeek/MiMo/Qwen live smoke pass.

## Scope

This pass reviewed Codey as a product system, not only as a unit-tested library:

- code elegance and duplication
- local unit tests
- single-model live smoke
- two-model writer/reviewer live smoke
- single-model self-bootstrap on a broken temporary Codey copy
- two-model self-bootstrap on a broken temporary Codey copy

All bootstrap tests used temporary copies. The main repository was not used as the repair target.

## Code Review

The current architecture is still reasonably clean:

- `agent.py` remains provider-independent.
- web page selectors stay isolated in provider drivers.
- two-model review lives in `review.py` and `task_runner.py`, without adding a group-chat UI.
- snapshot diff / restore stays separate in `changes.py`.

I did not extract the repeated `_visible_locator` helpers from MiMo and Qwen. They look similar, but keeping provider DOM code local is currently more robust: if one website changes, the repair stays in one adapter.

Small issues found and fixed:

- Qwen could edit a file and then try to finish without running tests, even when the task explicitly requested tests.
- Qwen could still echo website-side "tool does not exist" noise.
- MiMo could produce invalid JSON when an `old_string` contained unescaped quotes.

Fixes:

- `agent.py` now requires a successful `run` after file writes when the user asked for verification.
- `json_codec.py` now states earlier and more explicitly that tool names are local-runner JSON commands, not website tools.
- `json_codec.py` now reminds models to escape JSON strings correctly, or use full-file `content` when exact replacement strings are hard to escape.
- Added `tools/bootstrap_smoke.py` so self-bootstrap checks are repeatable.

## Local Tests

```text
python -B -m unittest
Ran 176 tests
OK
```

`git diff --check` passed.

## Live Smoke

| Mode | Result | Notes |
|---|---:|---|
| DeepSeek single create | Pass | Created code and tests, ran unittest, finished cleanly |
| MiMo single edit | Pass | Fixed bug, ran unittest, no upload-button misclick |
| Qwen single edit | Pass | After protocol fix, no "tool does not exist" noise; ran unittest before done |
| DeepSeek writer + MiMo reviewer | Pass | Writer fixed bug; reviewer approved |
| MiMo writer + DeepSeek reviewer | Pass | Reverse pair approved |
| Qwen writer + DeepSeek reviewer | Pass | Qwen entered review chain cleanly |

## Bootstrap Smoke

Bug injected into temporary Codey copies:

```text
difflib.unified_diff(after_lines, before_lines, ...)
```

Expected fix:

```text
difflib.unified_diff(before_lines, after_lines, ...)
```

| Mode | Result | Turns | Final full tests |
|---|---:|---:|---:|
| DeepSeek single | Pass | 5 | 160 OK |
| MiMo single | Pass | 5 | 160 OK |
| Qwen single | Pass | 5 | 160 OK |
| DeepSeek writer + MiMo reviewer | Pass | 5 | 160 OK |
| MiMo writer + DeepSeek reviewer | Pass | 5 after prompt fix | 160 OK |
| Qwen writer + DeepSeek reviewer | Pass | 5 | 160 OK |

## Does Two-Model Help?

For this specific injected bug, single-model repair was already strong: all three models fixed it in about five turns. The two-model path did not reduce writer turns because the bug was simple and tests were clear.

The advantage is confidence and failure recovery:

- reviewer sees the actual diff, not just the writer's summary
- reviewer can catch concrete mistakes before the user accepts the result
- if no different reviewer is available, Codey can try same-model self-review
  before falling back to the single-model result
- no extra UI switch is exposed to beginners

So the two-model feature is useful, but it should stay quiet and automatic. It is a safety layer, not a new product surface.

## Residual Risks

- Web pages can change DOM structure and break provider drivers.
- DeepSeek sometimes adds prose before JSON; the parser tolerates this.
- Web models can still be verbose or choose larger edits than a human would.
- Functional UI assertions and screenshot capture are automated, but there is
  no pixel-diff visual regression baseline yet.

## Passive Provider Supervisor and Writer Takeover

Deterministic local fault injection covers:

- typed provider action failures and stale-diagnostic clearing
- bounded health persistence, circuit cooldown, login/challenge state, and
  corrupt-state fallback
- data-free half-open canary with one-attempt behavior
- Writer takeover after edits using the current work checkpoint
- invalidating inherited green checks when a recorded file hash changes
- strict fresh chats, two-switch limit, shared turn budget, Stop priority, and
  first-rescue failure followed by a second sibling
- open-tab-first deterministic selection and health filtering for Doctor,
  hidden advisors, and Reviewer
- no takeover for ordinary local Python exceptions or agent stop reasons

Live Edge/CDP fault injection was run after the deterministic control-plane
tests. Each target's production message-box and send-button selectors were
replaced in memory, send-button heuristic selection was disabled, and a
healthy sibling selected among bounded DOM candidates. Recovery state used a
temporary store and did not modify the user's provider controls.

| Target | Sibling | Candidates | First recovery | Persisted reuse | Result |
|---|---|---:|---:|---:|---|
| DeepSeek | MiMo | 5 | 30.05s | 5.50s | provisional -> active |
| Qwen | DeepSeek | 2 | 45.20s | 6.08s | provisional -> active |
| MiMo | DeepSeek | 8 | 20.52s | 12.25s | provisional -> active |
| GLM | DeepSeek | 2 | 46.56s | 7.73s | provisional -> active |

The smoke exposed and closed two real recovery gaps before the final pass:

- Qwen could submit and receive a reply but lost the recovered send-button
  locator before its generation-complete check. Transaction-local locators now
  survive through staged validation and are cleared on success, abort, or
  explicit rejection; they are never persisted or reused across pages.
- GLM's send control is a `div.enter` without button semantics and was absent
  from bounded discovery. Discovery now admits only explicit send/submit/enter
  class candidates, with exact-token handling so `center` cannot impersonate
  `enter`.

## Release Notes

Version `0.1.3` is a durable model-browser release:

- model Edge/CDP browser is treated as long-lived user state
- Codey UI restarts do not intentionally close the model browser
- provider connections first reuse existing CDP browsers and model tabs
- if no usable CDP browser exists, Codey opens a new one automatically
- the last working CDP port is saved for quiet reuse after process restart
- no extra UI notification is shown for this recovery path

Version `0.1.2` was a usability and provider-status release:

- live green/gray model availability dots
- UI sends can still auto-open missing model pages
- connected models turn green after successful attach/open
- composer now uses Enter to send and Shift+Enter for newline
- root `DESIGN.md` is the single UI design source
