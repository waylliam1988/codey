# Research repair verification, 2026-10-30

## Phase-specific completion protocol

The initial native prompt and repair prompt now derive their completion guidance
and example from the current frozen `done` schema. Research requires both
`summary` and `report_sections`; evidence acquisition keeps its summary contract.
The validator still rejects summary-only research reports.

Live run: `artifacts/research-phase-web-qwen27b-20261030-a`, run
`run_f0408239c8604a36968ccc543a7f522e`. It performed real searches, opened official
documentation, saved eight findings and completed synthesis and review. Both
writer windows returned structured report fields, and the earlier final-window
summary-only loop did not occur. This is protocol execution evidence, not semantic
acceptance: manual inspection found unsupported finding scope and omitted version
and workload requirements despite the same-model assessment passing.

The controlled frozen request remains a negative result:
`artifacts/research-phase-guidance-raw-20261030-c` reproduces summary-only output
with the new phase guidance. `native-generation.json` captures the local server's
`/api/extra/generate/check` result immediately after the HTTP response. The raw
generation itself has only `<parameter=summary>`, matching the HTTP arguments.
For this reproduction the field was absent before native parsing. A prompt fix
alone has not made this deployment reliably obey the schema.

Validation before the protocol commit: 59 tests passed in the structured-report,
native-codec, contract-profile and protocol-contract selection. The protocol-contract
file has nine pre-existing failures, reproduced with the parent prompt/preparation
modules (19 passed, nine failed); these assert old coercion and error wording.
The semantic review/structured-report selection additionally passed all 36 tests.
Ruff,
mypy, Pyrefly and ty passed. Previously documented prompt hash and wording tests
still fail on the parent revision and were not rewritten to conceal the baseline.
No full pytest or push was performed.

## Browser route shutdown

The first browser fix (`fa797f9a`) detached page routes with
`unroute_all(behavior="ignoreErrors")` before closing the connection. The short
probe below passed, but subsequent full Research attempts still reproduced an
unobserved TargetClosedError. It was a partial fix, not proof that all shutdown
paths were repaired. Shared search/fetch pages are released once; repeated close
remains harmless.

The real production browser fetched `https://www.sqlite.org/wal.html` successfully
(31,603 text characters), then closed twice without TargetClosedError. Node still
reported its unrelated DEP0169 deprecation warning. The focused regression checks
route teardown precedes context close. Browser tests, finding tests and structured
report tests passed together (47 tests); Ruff and the three production type checks
passed. This verifies the shutdown path, not research content correctness.

## Question ownership and assessment scope

Research investigation calls explicitly select a registered question_ref. The
existing settled tool receipt owns the argument, and the ordinary live/recovery
projection derives aN ownership and the actual read passage refs from it. There is
no second runtime, receipt store, or knowledge write for question adoption.
Strict writer and bounded investigation phases require the argument; evidence-only
done still needs only its acquisition summary. An investigation can register a
plan when none exists, rather than inventing ownership for unregistered questions.

save_finding requires every selected passage to have been read for its question.
Coverage rejects aN/fN from a different question. Re-reading the same exact finding
for another question creates another adoption label without a new knowledge
revision, preserving its original acquisition metadata. Inferences also require
premises adopted for their question. The report's pinned question binding must
still match when its assessment basis is constructed.

The original task is losslessly segmented into frozen rN text spans with offsets.
These are not claimed to be semantic requirement decomposition. Assessment must
judge every span, every adopted finding text unit, every paragraph text unit, and
every investigation selected in coverage exactly once. Supported finding units
need exact quotes in their selected evidence; supported paragraph units need
actual cited finding units that the assessment also supports. Irrelevant or
uncertain investigation judgments block acceptance. All these checks use the
existing assessment submission and publication path.

Source quality is generated solely from pinned source metadata. The model no
longer submits a second source_quality prose section. Publication/release fields
remain unknown without explicit metadata; retrieval time cannot supply either.
Existing heuristic source classifications are labeled as hints. The model can
still make wrong version/source claims elsewhere, so this does not eliminate the
need to assess the report prose.

These changes enforce ownership and make the complete assessment scope explicit.
They cannot mechanically prove natural-language entailment. A same-model
assessment remains fallible, including comparator substitution, wrong conditions,
and misclassification within a text span. A valid quote alone is not semantic
certification.

### First full live ownership attempt

`artifacts/research-ownership-live-20261030`, run
`run_63f5fabafec14482aec622ec42212f14`, used the original WAL question, real
production browser and Qwen 27B with thinking off. It demonstrated rejection of
cross-question passages/findings and later of a wrong aN used for q1. The model
repeated invalid reports, performed follow-up source searches and reads, and saved
new f10/f11 version findings. The acquisition child exposed an omitted-question
loophole; the subsequent fix extends the requirement to that phase too.

The last report reached assessment, but its generation exceeded the diagnostic
300-second deadline. No complete assessment or accepted final research result
was produced. A partial server generation identified the cleanup/recovery BUSY
clauses as uncertain; this is not a committed judgment. Production source changed
during this attempt, so it is diagnostic evidence, not frozen-source validation.
The next fresh run records runtime_implementation_id and uses the production
600-second deadline.

The failed run has no pending effects, but its operation deliberately retains
writer_running: the last provider receipt has sent_state=maybe_sent after
GenerationUnknownError. prepare_operation_settlement refuses to erase unknown
external outcomes. This is the existing recovery boundary, not evidence that
the operation reached terminal; the historical record was not rewritten.

### Frozen-source small live attempt

`artifacts/research-ownership-small-final-20261030`, run
`run_806e23f02ce7417ba5986889baef719e`, records the frozen production
implementation id and uses the 600-second provider deadline. Qwen opened and
paginated the real official WAL page, bound reads and three findings to q1..q3,
and returned structured reports. It did not execute web_search despite the task
request; opening a URL or source_search is not a web search.

The final assessment failed its three permitted submissions: an invented gap
without matching coverage, then paragraph support selecting nonexistent or
uncited finding units. Eight task judgments also omitted required question/path
bindings. No accepted assessment or synthesis was published. The final stop is
research_review_incomplete, durable task failed, operation terminal, 37 effects
settled and none pending. Generation durations for the three assessments were
347.141, 441.328 and 448.610 seconds. This is a demonstrated rejection of invalid
assessment, not a successful end-to-end research result.

### Controlled comparator calibration

The production assessment kernel/provider processed two synthetic inputs in
`artifacts/research-support-calibration-{negative,positive}-live-20261031`.
Both have the same original excerpt and correct finding: alternating therapy
versus high-dose combination therapy. The negative report substitutes
capecitabine alone; the positive report retains the correct comparator.

The negative assessment completed, rejected summary/evidence/coverage units as
unsupported, and published no synthesis. The positive completed with no issues
and one synthesis. These are explicitly synthetic reviewer calibration results,
not new clinical research or a web end-to-end test. The negative reason also
imprecisely reuses the excerpt's capecitabine-versus-MTD p-value; that p-value is
not a test of alternating therapy versus capecitabine alone. The final rejection
is correct, but its explanation remains fallible.

After the small live failure, invalid paragraph support errors now name the
exact path/unit, rejected selection and actual available finding units. Invalid
gap errors list the frozen gap paths. Task binding errors identify rN. Validation
is unchanged; no invalid assessment is filled in or automatically approved.
The live-web manual runner now declares web_search as an existing required tool
so a URL read alone cannot satisfy its search requirement.

### Minimal live failures and deterministic corrections, 2026-10-31

`artifacts/research-ownership-minimal-20261031`, run
`run_329751c56fc24223b96ff5dc6a051571`, searched, read and saved f1 for q1.
Its coverage selected a1/a2, but the assessor repeatedly submitted all a1..a5.
The exact-set check correctly refused these submissions. The frozen assessment
basis now projects only investigations selected by coverage, instead of showing
unused history beside the selected scope. Validation errors show expected and
received IDs; they do not discard extra judgments silently.

`artifacts/research-ownership-minimal-final-20261031`, run
`run_b74ed54a71e04bf7986187a655a06cec`, exposed a compiler defect: structured
coverage.findings selected f1 and rendering added its source citation, but the
compiled paragraph binding used only inline answer citations. A valid answer
without an inline [f1] therefore had no available finding units at assessment.
The compiler now derives that answer's binding from the already validated
coverage.findings. Inline citations must still be a subset of those findings.
The regression compiles real saved knowledge and builds the production review
basis, with and without inline labels, and rejects conflicting inline labels.
No second citation parser was added to assessment.

Both minimal attempts ended research_review_incomplete with no accepted report.
The latter also produced a quote outside the finding's selected excerpt, which
was rejected. These are not successful fresh-web acceptance results.

With PYTHONASYNCIODEBUG enabled, the latter attempt captured the unobserved
future's creation in Playwright Page._on_route's background interception update.
ignoreErrors returns without draining active handlers; disconnecting pw.stop()
can then terminate their newly scheduled network-interception requests. The
release now uses the public unroute_all(behavior="wait") lifecycle API before
disconnecting. It does not filter exceptions, patch Playwright internals, or
close shared user tabs. The fresh-web rerun checks this longer-lived path too.

`artifacts/research-ownership-minimal-fixed-20261031`, run
`run_c5c18f90a0b8480bb65a2677712a269a`, confirmed the coverage binding correction:
the assessor could select f1 units for the answer without an inline [f1]. All
three submissions instead failed on a quote that changed source line wrapping
to spaces. The same words, case and punctuation occurred in the selected
excerpt. Quote admission now compares whitespace-normalized contiguous strings,
while retaining the submitted assessment and original excerpt unchanged. Tests
reject changed words/numbers/case, omitted material, ellipses and removed word
boundaries. This normalizes layout only; it does not assess entailment.

The run's task failed, operation is terminal, all 34 effects settled and none
pending. Shutdown under asyncio debug produced no TargetClosedError. This
resource correction is committed separately as `33787196`.

The invalid submissions also reveal a semantic error that whitespace repair
cannot fix: the assessor approved a report saying the original document did not
discuss performance/history/workload, based only on their absence from the
selected excerpt. It also failed to challenge a reported same-host condition
outside that excerpt. These judgments never became an accepted report in this
run. Writer and assessment guidance now make the selected-excerpt versus
whole-source boundary explicit, but prompt wording is not a reliability proof.

Validation at this point: the broader relevant selection passed 307 tests;
29 browser-focused tests passed separately. The quote/assessment changes passed
58 related tests, with 52 scope/guidance tests after the wording refinement.
Counts overlap and are not summed. Ruff, mypy (574 source files), Pyrefly
(0 errors, 35 suppressed, 80 warnings), ty and 714 runtime resources passed.
No full pytest was run.

### Existing test failures

A detached baseline checkout at `dee5bf7b` reproduced 12 failures in
`test_research.py`, `test_recovery_restores_research_ledger.py`, and
`test_research_contract_profiles.py` (157 passed). Ten assert old boundary
fixtures/prompt behavior and two expect an unstructured report to pass the strict
completion gate. Those gates were not weakened. The nine older protocol-contract
failures documented above are a separate baseline selection; counts overlap and
are not a full-suite failure total. No full pytest was run.

## 2026-10-31: review and commit of the pending changes

Reviewed the 36 pending files against `33787196`. The interdependent question
ownership, report contract and review-scope changes are committed together in
`7d3ca2ac`; they continue to use the existing driver, effect receipts and knowledge
transactions. Source quality is application output, and the assessment still
has the explicit `same_provider_model_assessment` identity. Structural acceptance
does not establish that this model's semantic judgments are correct.

The review found and fixed two diagnostic-script defects in `2324ce22`:

- A failed optional `/api/extra/generate/check` request could discard an already
  successful provider response. Both local runners now record diagnostic failures
  separately, including HTTP errors, timeouts, incomplete HTTP responses and invalid
  JSON, while retaining the original response.
- Imported reports restored their findings but omitted the actual aN investigation
  receipts selected by coverage. Archive review now takes a read-only SQLite backup
  and uses the production receipt decoder and observation projection to restore
  those facts. It neither re-executes acquisition nor substitutes invented attempts.
  The original accepted report is imported unchanged; missing metadata is no longer
  patched into a different compilation. Archives must use the current contract.

Validation in this review, on the committed production code:

- 170 passed across question ownership, review scope, semantic review, findings,
  reference coverage, structured reports, persistent lifecycle, accepted queue and
  durable continuation/restart tests.
- 6 diagnostic regressions passed. The archive test selects real a1/a2 receipts
  from a shared-driver run, invokes the actual `--prepare-only` entry, checks the
  restored review basis and unchanged compilation digest, and verifies no new
  acquisition calls occurred.
- A separate contract, observation, generic tool-validation and proof-quality
  selection produced 124 passed, 10 failed and 2 passing subtests. A detached
  `33787196` checkout reproduced the same 10 failures in
  `test_research_protocol_contract.py` and `test_research_proof_quality.py`
  (35 passed). These are existing failures, not a green full-suite claim.
- Ruff passed for production and the changed/new Python tests and runners;
  mypy passed for 574 source files; Pyrefly reported 0 errors (35 suppressed,
  80 warnings); ty passed. The published runtime inventory validated 714 files.

The three current-code selections contain 300 passing tests in total; baseline
runs and repeated diagnostic runs are excluded from that count. No full pytest or
new local-model/web experiment was run in this review. Existing artifact narratives
above remain records of their original runs, not results for these commits.
