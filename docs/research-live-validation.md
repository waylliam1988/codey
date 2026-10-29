# Local Research validation, 2026-10-30

Work in progress. Do not infer a live acceptance pass from deterministic tests.

## Continuation: marker strings and malformed HTTP framing

- `research-live-wal-member-repair-20261030` searched and opened a real page,
  but Gemma marker-delimited strings containing raw newlines were rejected by
  the local adapter. Stopped that unsuccessful diagnostic before a fresh run.
  Marker strings now permit literal control characters while retaining JSON
  escape semantics. Ordinary JSON strings still reject raw control characters;
  malformed escapes, duplicate keys and incomplete frames remain rejected.
  Nested evidence/report fields and exact text preservation have regression
  tests (23 codec/gate tests passed; Ruff and production mypy passed).
- `research-live-wal-multiline-20261030` searched and opened the official SQLite
  WAL page, then failed on malformed generation HTTP framing: a complete HTTP
  header appeared inside the response body, truncating the actual JSON under
  the outer Content-Length. The unknown generation was not resent or decoded
  by stripping headers. This is not a live acceptance pass.
- Upstream 1.117.1 reuses one handler instance per server thread. Python's
  `flush_headers` clears its buffer only after a successful socket write; a
  disconnected request can leave that buffer on the reused handler. This is a
  plausible explanation for stale nested headers after earlier aborted native
  requests, not a proven cause of every malformed response. Restarted only the
  owned server from its unchanged config to obtain a clean validation baseline;
  logs: `artifacts/kobold-research-clean-server*.txt`. No external software
  source/config changes or generation retries were introduced.

### Reasoning text must preserve text-protocol identity

- The clean-server natural run (`research-live-wal-clean-20261030`) executed
  actual searches, opened two official pages and saved a direct concurrency
  quote. It then repeated an incomplete done frame with summary nested inside
  report_sections. It failed `protocol`, not research acceptance. Its prose also
  incorrectly described BEGIN CONCURRENT commits as simultaneous; do not reuse
  it as an accepted answer.
- Local adapter errors now retain bounded tool-specific diagnostics in the
  existing raw response metadata. They survive durable receipt round trips;
  malformed calls still cannot execute. Done repair explains the root summary
  and supplies all five structured sections instead of just a generic error.
- `research-three-stage-feedback-20261030` exposed an independent production
  defect with thinking enabled: valid text JSON requests plus reasoning were
  wrapped as a native AssistantTurn with no calls, so the kernel treated each
  search request as a final answer. The run reached max_turns without executing
  a search. API/Zen wrappers now explicitly mark these as text-protocol replies;
  the shared decoder uses that identity after receipt recovery and still checks
  the frozen ToolSpec/policy. Unmarked native prose/JSON is not promoted into
  an executable text request. Reasoning stays separate and visible.
- Lexical coverage no longer treats nested object/array examples, inline/fenced
  code or dotted API fields as research topics. Long unquoted Chinese clauses
  are not exact-match keywords; explicit quoted topics remain available. This
  is a limited lexical check, not proof that a paraphrase is semantically valid.
- Focused verification: 71 protocol/prompt/proof/planner tests and 22 completion,
  merge/native-contract tests passed; a further 53 reasoning/receipt/codec/term
  tests passed after adding protocol identity and recovery regressions. Sets
  overlap. A fresh three-process thinking-enabled run is now validating the fix;
  its first search has actually executed. No full live pass is claimed yet.

### Bounded reasoning and report repair

- `research-three-stage-reasoning-protocol-20261030` did execute search, opening,
  source_search/open_hit and both knowledge writes. Exact concurrent-reader and
  single-writer quotes, aliases and both condition states were saved correctly.
  Its first final report omitted numbered source rows; on repair, unrestricted
  thinking repeated self-checks until the 5,000-token output limit. It failed;
  stored evidence is useful but this is not a completed three-stage result.
- The manual probe now exposes the existing production reasoning-effort option
  (`--reasoning-effort low`, mutually exclusive with --thinking), records it and
  propagates it to every fresh process. No production default is changed.
- Question-term hints prioritize explicit interrogative sentences when present,
  rather than delivery/storage instructions after them. Long or interrogative
  Chinese clauses are not required verbatim. Semantic evidence checks and the
  independent persisted-condition acceptance requirements remain unchanged.
- Formatting/reference failures now request repair of the reported defect;
  they no longer simultaneously demand fresh research. When all sections are
  present, feedback identifies the numbered source-row format instead of
  displaying an empty missing-headings list. These are prompt fixes, not
  automatic rewriting/acceptance of submitted reports.
- Added an API -> reasoned JSON -> shared-kernel tool execution integration
  regression. A focused 130-test set (research/proof/planner/merge/completion,
  reasoning/codec/durable receipts/Zen) passed. Ruff and production mypy passed.
- Active fresh probe: `research-three-stage-low-20261030` (low reasoning,
  16 turns per stage). First stage has searched, opened and saved evidence;
  final reports/recall/revision still need inspection.

## Continuation: explicit evidence and backend parser diagnosis

- The earlier `research-live-wal-action-20261030` returned `done`, but manual
  inspection rejected it: its reconstructed answer omitted major questions and
  its evidence included site navigation. It is **not** a quality acceptance pass.
- Removed the omitted-evidence fallback that guessed a source passage and marked
  it as support. Sourced findings now require explicit matching excerpts. Source
  catalog notes can be saved without creating a supporting claim. Updated legacy
  write fixtures to supply actual opened observations instead of a bare URL.
- Condition validation now identifies the exact failing path and shows the
  nested name-to-object mapping, including an unknown/not-extracted condition.
  The native schema describes the same shape. Invalid conditions cannot publish
  a partial note or claim.
- Evidence merging no longer promotes a failed/budget-stopped investigation to
  `done`. Minimal reconstruction is `partial`, and changed summaries discard
  the old completion proof. More evidence alone does not prove answer completion.
- Clarified native summary/report_sections alternatives, quoted enum repair, and
  source-locator offsets versus receipt offsets. Locator feedback gives a real
  open_hit example from recorded hit targets.
- `research-three-stage-counted-20261030`: failed no_progress after repeated flat
  conditions. `research-three-stage-explicit-20261030`: first four turns ran, then
  a generation timed out after the initial SSE role event. No request was resent.
  py-spy on the actual KoboldCpp process showed its active thread stuck in
  `read_object -> fix_unquoted_keys -> parse_gemma4 -> repack_toolcall_tags`.
  Backend inference had already completed (399 tokens, about 12.6 s); the parser
  remained active for several minutes. This is a separate server-side hang.
- `research-three-stage-text-20261030` queued behind that stuck server thread,
  so it also failed; this is not a text-protocol capability result. Restarted
  only the wedged KoboldCpp server using its saved `Gemma4-12B-QAT/2.kcpps`
  settings and `--skiplauncher`, with hidden window and logs under artifacts.
  User config files and project dependency declarations were not modified.
- Verification so far: 216 research/storage/merge/protocol tests plus 7 subtests;
  140 expanded contract/native-guidance/continuation/report tests. Ruff, mypy
  (551 files), Pyrefly (same existing warnings), and ty passed. Intentional prompt
  snapshots/assertions were reviewed for the report_sections protocol change.

Next: fresh text-protocol three-process probe, actual SQLite browser research,
and actual non-software research. Inspect report contents and stored evidence;
none of these fresh live acceptance outcomes has been established yet.

### Further live repairs

- Quote admission accepted whitespace/case normalization, but ResearchRecord's
  char locator required a literal match. Added contiguous whitespace-aware
  source offsets (same matching rule, no fuzzy replacement); verified against
  original multiline text and rejected altered/ellipsis passages.
- The text probe after restart stored both direct quotes and the alias, but
  omitted the requested unknown workload condition. A shorter guided task now
  gives the exact condition mapping; acceptance requirements were not weakened.
  Its later process exited with Windows access-violation status 3221225477 before
  a final result. Root cause is unconfirmed. Periodic CPython thread dumps in
  the manual harness are now opt-in (`--diagnostic-stacks`), not on every run.
- Real browser run `research-live-wal-text-20261030` successfully searched, then
  token-count admission rejected malformed metadata JSON. A separate read-only
  request captured HTTP response headers embedded in the `/api/v1/model` body;
  subsequent reads returned valid JSON. Metadata/tokenizer reads now get exactly
  one fresh retry on JSON decoding failure, within the existing deadline. They
  still fail closed on repeated malformed responses, changed model/window or
  invalid counts. This retry cannot send inference or any research tool effect.
- `research-live-wal-text-count-repair-20261030` repeated the same malformed
  unnamed search-string fragment for 20 turns. JSON repair now distinguishes
  missing member colons/implicit concatenation from quote-escaping errors.
  Initial research and investigations use the shared stagnant-turn bound (5)
  for invalid calls, rather than exhausting a long research budget on syntax.
- `report_sections` now rejects a duplicated full report in summary, including
  literal escaped headings, with specific repair guidance; it does not strip or
  unescape content. Non-heading literal backslashes remain preserved.
- New regression sets: 49 locator/model/proof/merge tests; 27 counting/retry/
  receipts tests; 62 syntax/research/continuation/report tests. Latest Ruff,
  mypy, Pyrefly and ty all passed with unchanged configurations.
- Fresh natural browser run `research-live-wal-member-repair-20261030` is active.
  It has executed actual search and page opening after the syntax repair.
  This is progress, not yet a completed research acceptance result.

## Environment

- KoboldCpp 1.117.1, localhost:5001, Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M.
- Windows console text selection paused the server inside `utfprint`, before model
  inference. Confirmed by py-spy stack and GetConsoleSelectionInfo (flags 7).
  Clearing selection restored requests. Codey does not manipulate user consoles.
- Diagnostic `py-spy` was installed in the development venv only, not dependencies.
- Live probes use isolated state under ignored `artifacts/`, the production
  headless entry, provider, shared kernel and browser search. No user settings changed.

## Findings and fixes

- Wait for search result elements after DOM navigation, bounded by the search
  deadline, instead of treating a still-loading results page as an empty result.
- Reject HTTP error responses before acquiring a citable source, including 404
  pages whose body contains readable text. Cover browser and HTTP fallback.
- Preserve HTML s/del/strike as explicitly deleted text. SQLite's current WAL
  page contains struck-through historical large-transaction restrictions; losing
  this markup let the model report those as current guidance.
- Reject a supplied evidence excerpt that does not match the opened source.
  The previous fallback silently substituted another passage, which could
  support a different proposition. Identify the failing item and require a
  contiguous quote, without manufacturing a replacement.
- Completion feedback now explains missing sections, literal newline escapes,
  evidence requirements and structured knowledge ids even when required checks
  reject early. Formatting repair no longer instructs unconditional extra reads.
- Distinguish search hit ids, opened source ids, result_ref receipt ids, artifact
  handles and knowledge object ids in model guidance and error feedback.
- Add optional done.report_sections (five arrays of paragraphs, with summary as
  the conclusion). A pure renderer supplies the existing six Markdown headings.
  The same source/evidence/citation/quality gate runs on the resulting text.
  No text unescaping, guessed content or new execution path is introduced.

## Live attempts retained

- `research-live-sqlite-20261030`: real Bing search and official page opened;
  failed completion. Found the 404, receipt-id and deleted-text issues above.
- `research-three-stage-fixed-20261030`: text protocol stored evidence but
  repeatedly emitted malformed done JSON. Stopped this diagnostic after repeats;
  not an acceptance pass. Its second claim's quote was only topically related.
- Minimal native newline diagnostic reproduced a literal backslash+n returned
  in a summary despite asking for actual line breaks. KoboldCpp's Gemma parser
  escapes backslashes in its raw native string syntax. No adapter unescaping was
  added because literal escapes may be intentional content.
- `research-three-stage-structured-20261030`: native + reasoning enabled;
  initial attempt hit wrong ids and noncontiguous quotes;
  shared follow-up began source_search. No final success claimed.
- `research-three-stage-streamed-20261030`: native stream without reasoning;
  repeated malformed Gemma string arguments, then a missing required body.
  Not an acceptance pass.
- `research-three-stage-jsonparts-20261030`: text mode produced a repetitive
  tool frame and exhausted output tokens. Not an acceptance pass.
- `research-native-thinking-20261030`: native stream + reasoning, failed.
  Initial report passed the mechanical gate, but its second excerpt did not
  directly establish a single writer. The ordinary shared investigation then
  explicitly identified that evidence gap. Inspect its final persisted results
  before treating this run as a research-quality pass. Its subsequent request
  failed during context compaction, not browser acquisition or SQLite commit.

## Token-count correction

The manual probe still used ApiProvider's generic estimated request counter,
whereas the production Local connection admits KoboldCpp's full Jinja/tokenizer
counter. Recounting the largest captured request produced 29,506 estimated
tokens versus 19,341 actual tokens. At the probe's 27,268-token input limit this
unnecessarily invoked compaction; the auxiliary model then exhausted its output
budget while repeating historical investigation plans. The probe now uses the
same admitted_counter/counter_for and usage configuration as Local. No production
context limit, cancellation, unknown-request handling or model setting was changed.

`research-live-wal-counted-20261030` is the first real browser research rerun with
this correction; its Python process ended without a final task/result receipt
after the second streamed request. A Playwright EPIPE appeared after process
termination. No Windows Python crash event was found; the terminating cause is
not established. This is not a successful or safely replayable model request.
The two checked-in research_live questions
contain research requirements, without tool schemas or prescribed call sequences.

## Real browser action run

`research-live-wal-action-20261030` uses native stream without reasoning and the
Local production temperature (0.3). It progressed through real search/open/write
actions, exposing two further defects fixed in this continuation:

- Chinese challenge text headed `页面安全校验中` was admitted as an ordinary
  opened source. The short-challenge filter now recognizes that page and related
  browser-verification markers, in both browser and HTTP fallback. Long research
  articles discussing verification retain the existing length-based allowance.
- Missing `sources` feedback told the model to reopen an already opened page or
  switch to hypothesis. It now names the omitted fields and observed URLs, and
  explicitly forbids relabelling a sourced finding to bypass its citation.

The model also spent searches on derivative pages despite the question asking
for official sources. Strict guidance now explains refining with a site: filter
or following original citations, and choosing one useful action rather than
repeatedly rehearsing a complete answer. Native knowledge_write declares that
body remains required even when evidence is supplied. These changes still need
their fresh-process live rerun; do not attribute them to the already-running run.

Probe acceptance now requires direct single-writer and concurrent-reader quotes,
the requested alias and an explicit unknown workload condition for the controlled
first stage. It exports final report text and the exact persisted knowledge and
evidence versions for every completed stage, including reported failures.

## Focused verification in this continuation

- 70 acquisition/report/protocol tests passed.
- Expansion: 179 passed, one legacy prompt snapshot differed from the current
  contract. Reviewed and updated only the two research system prompt fixtures;
  the subsequent 30-test prompt/contract/report set passed. Counts overlap.
- Production Ruff, strict mypy (551 files), Pyrefly and ty passed. Pyrefly retains
  77 warnings and 35 existing suppressions; no checker settings were relaxed.
- Live harness now records stage settings and persisted claims with their exact
  referenced evidence versions for review. Mechanical success does not certify
  semantic support or answer completeness.
- After the Chinese challenge and citation-feedback fixes: 186 acquisition,
  source, knowledge acceptance and report tests plus 7 subtests passed. Full
  production Ruff/mypy/Pyrefly/ty passed again. Existing warnings unchanged.

## Remaining work

1. Finish successful real three-process research / recall / revision validation,
   inspecting actual quotes and persisted conditions, not just exit status.
2. Rerun the real SQLite browser task after fixes and assess answer correctness.
3. Run a real non-software browser research task to check generalization.
4. Complete targeted recovery/protocol/acquisition checks and production Ruff,
   mypy, Pyrefly and ty; update intentional prompt fixtures after review.
5. Record exact successful and failed artifacts and commit remaining changes.

Frontend unchanged in this iteration. Full pytest is intentionally not requested.


## Continuation: preserve the answer and inspect actual thinking

The binding-medium probe was stopped after it persisted a single-writer claim
with a quote that only established concurrent reading and writing. It is not an
acceptance pass. Its missing-fields/protocol repairs also consumed many turns.

Completion now preserves the submitted answer rather than silently reducing it
to supported sentences before checking it. A failed claim binding reports the
unbound wording and the saved evidence claims. This is a text-binding check,
not a semantic certification that the cited excerpt entails the statement.

The probe previously treated `--thinking` and `--reasoning-effort` as alternatives.
Captured payloads proved that effort alone did not enable Gemma reasoning. They
are now independent, matching the production provider. The fresh
`research-three-stage-thinking-low-20261030` run sends both
`enable_thinking=true` and `reasoning_effort=low`; outcomes remain pending.

Verification: 62 completion/protocol/prompt/merge tests and 17 subtests passed;
Ruff, mypy (551 files), Pyrefly and ty passed. Existing checker warnings unchanged.


A deterministic follow-up evidence patch now always returns `partial` and clears
completion proof. Previously the nonminimal branch could change a completed
report and preserve `done` without running the shared completion gate again.
The patch exposes `ungated_evidence_patch`; a no-op still returns the original
result unchanged. Regression coverage includes ordinary and minimal merges,
idempotency and pipeline selection. The focused merge/pipeline/golden set passed
37 tests. Reviewed golden prompts already match; no fixture update was needed.


The first explicitly enabled/bounded thinking run reached real tool execution,
then received HTTP status/headers embedded inside the generation JSON body.
It was not resent. A clean server was started with the original user config;
`research-three-stage-thinking-clean-20261030` is the next isolated run.
KoboldCpp 1.117.1's server source reuses a handler object per worker; Python's
header buffer survives a failed flush. This explains a plausible connection
with earlier interrupted requests, but remains a diagnosis to validate rather
than a general server fix. No external software/configuration was edited.

Transport now reports this exact framing defect with a bounded diagnostic and
`response_http_headers_in_body`, while keeping the unknown-generation boundary.
It neither strips headers nor retries inference. The corresponding one-send
regression and 29 focused transport tests passed. Separately, 53 knowledge,
shared-kernel, Local codec and subprocess continuation tests passed.


Live Gemma repeatedly supplied exact evidence with its source_url while omitting
the duplicate note-level sources list, even after specific feedback. For new
notes only, omitted sources now catalogue the caller's explicit evidence URLs.
Explicit empty sources and existing-note update semantics are unchanged. All
ordinary opened-source, excerpt and evidence checks still run before commit.
This is locator copying, not inferred provenance or semantic support.

The contract documents this behavior. Regressions prove valid evidence is saved
with the URL, unopened URLs and invented excerpts are blocked, and an explicitly
empty list is not repaired. The production-entry cross-session knowledge test
also exercises the omission. 48 repair/contract/golden and 20 knowledge/evidence/
merge tests passed. Only the two reviewed research prompt snapshots changed;
Ruff passed. This change was made after the clean probe's first process loaded,
so its earlier omission failures are retained and not attributed to new code.


The clean thinking/low first stage returned production `done` at turn 12. Manual
inspection confirmed two separate claims with their correct direct official
quotes, both aliases and unknown workload conditions. Its report is short and
its limitations section is weak; this is the controlled lifecycle check, not
proof of broad research coverage. The manual validator initially rejected the
literal official wording `there can only be one writer at a time`, including
line breaks retained in stored evidence. It now normalizes whitespace and
recognizes that exact phrase. Saved results were rechecked without generating
or editing knowledge; `first-validation.json` records the post-hoc inspection.
Recall and revision continue as separate processes on the same isolated store.

Live report repair also exposed the fallback completion-contract message still
requesting fresh evidence when the only failing checks were report/reference
repairs. That branch now gives the same targeted repair instruction; unrelated
source/verification failures retain their original instruction. Not-applicable
checks do not count as failures for this feedback choice. 12 focused completion
tests passed, including preservation of an unbound claim and targeted feedback.


## Cross-session recall failures and repairs

The clean first stage is followed by retained `recall-attempt1` through
`recall-attempt4` artifacts. These are failures: Gemma returned proposed commit
JSON as report examples, sometimes after misspelling a long knowledge identity,
and no candidate was persisted. The absence of project edits meant the ordinary
completion checks could not prove this natural-language save instruction.
The harness correctly rejected these mechanically completed answers. The
second attempt also reused the failed conversation; stages now have distinct
session names and --session-id permits an explicitly fresh diagnostic retry.

Added common, permission-aware knowledge action guidance. It tells the model
to execute requested saves and await success, read versions before adopting or
revising, and retain candidate status. Read-only/denied capabilities are not
advertised. The reviewed task-guidance digest was intentionally updated.

The text-protocol kernel now asks for explicit delivery when prose contains a
knowledge write/commit request: either execute one bare request, or intentionally
deliver the example inside done.summary. It never executes extracted prose or
fenced examples. Native prose and JSON answer data remain non-executable. This
is a framing repair, not a semantic guarantee that an explicitly submitted done
satisfies every user instruction.

Missing-id errors now suggest up to three scoped literal-prefix candidates,
without selecting or reading one. Search supports bounded literal id prefixes;
identity, scope, revision checks and optimistic concurrency remain unchanged.
Regression coverage checks ambiguous public candidates, more than a page of
private candidates, and a missing historical revision. Private IDs are filtered
before the candidate limit. No fuzzy identity resolution was introduced.

33 decoder/knowledge/guidance tests and 66 production-entry/Local/API tests
passed; the earlier expanded guidance set passed 110 tests. Ruff, mypy and ty
passed. Pyrefly initially found two missing local list annotations; those were
added and its rerun passed with unchanged existing warnings/suppressions.
The current `research-recall-guarded` run is the first fresh process containing
both the framing check and missing-id suggestions; outcome remains pending.


## Explicit outcomes and accurate protocol repair (2026-10-30)

The fifth recall attempt read the correctly pinned old claim after a scoped
missing-id suggestion, but still displayed a commit example instead of saving
it. It failed acceptance; recall and revision have not yet passed. The earlier
prose framing heuristic was insufficient and has been removed. Answer examples
remain non-executable.

Task callers can now explicitly declare required_tools. The existing immutable
TaskPolicy ToolSuccessRequirement and shared completion gate verify successful
execution receipts. This does not grant permissions or infer obligations from
natural-language keywords. API/headless submission, durable dispatch/recovery
and forks retain the declaration. The manual recall/revision tasks declare read
and commit, matching their existing instructions. Regression coverage runs the
real headless entry: an example cannot complete, then an actual read/commit can.

Local marker syntax diagnostics now distinguish an unclosed outer argument
object from a bare enum. The prior generic quoting instruction caused repeated
incorrect repairs. No missing braces or argument values are supplied by Codey.

Verification: 113 focused entry, policy recovery, codec and durable tests passed.
Production Pyrefly and ty passed (existing warnings/suppressions unchanged);
Ruff and mypy also passed before this expanded run. Live medicine research is
still running and has searched/opened original sources; this is not acceptance.


## Live web acquisition and feedback diagnosis

The medicine run really opened Nature, Cancers/MDPI, a PMC preprint and a
ClinicalTrials.gov URL. Browser DOM inspection found the registry app-root empty
while a large aria-hidden glossary made acquisition look successful. Generic
HTML extraction now excludes hidden, aria-hidden and inline display/visibility
hidden subtrees. The captured page reduces from about 40 KB to "Show glossary";
the existing bounded settle/fallback rejects it as page_blank, so it cannot
become citable study evidence. Browser and HTTP gateway regressions cover this
shell and confirm that no opened-source/evidence ledger entry is created.
This repairs false content availability; it does not make the registry's JS
application load or certify study identity.

The same run proposed multiple writes without executing them, then repeatedly
returned an ungrounded report. Missing-evidence feedback now prioritizes one
actual sourced save instead of simultaneously telling the model to repair using
already-saved evidence. Common research guidance asks for exact passage capture
after reading and exact opened URLs. No treatment conclusions or quotes are
inserted by these changes; live report quality remains unaccepted.

The medicine run ended at turn 12 with an unknown generation outcome, exactly
180 seconds after send (WinError 10038 following the deadline socket shutdown).
It did not save evidence and failed acceptance. The manual probe had an
artificial 180-second request cap while production ApiProvider defaults to 600;
--provider-timeout now records/exposes the actual setting and defaults to the
production budget. This is a harness correction, not a production timeout fix
or a replay of the unknown request. The generated medical prose also contained
unsupported clinical generalizations and must not be treated as verified.

Focused checks for hidden source content, gateway, completion feedback, report
shape and knowledge lifecycle passed 63 tests; prompt ownership/golden/repair
checks passed 23 tests. Ruff, mypy (551 files), Pyrefly and ty passed.


The first explicit-outcome recall run (attempt6) correctly failed its completion
requirements but then repeated prose without action over several turns. It was
intentionally stopped; no successful read or inference commit was observed.
The owned Kobold process was restarted from the unchanged user config before
a fresh diagnostic session, rather than resending any in-flight request.

Runtime work observations now project pending tool requirements from the same
existing evaluator/receipts at each tool-result boundary. Successful execution
removes the reminder; a later answer-binding failure does not request reexecution.
Missing-id candidates now include complete explicit read requests, avoid echoing
the bad identity as the main instruction, retain scope filtering and never pick
or execute a candidate. Markdown thematic rules no longer become factual
claims; bold factual assertions still require evidence.

Verification of these repairs: 103 focused object/knowledge/outcome/answer/prompt
tests and 81 runtime/entry/feedback tests passed (sets overlap). Production Ruff,
mypy, Pyrefly and ty passed. The next live recall session uses thinking enabled
with medium effort, an eight-turn diagnostic bound and production request timeout.
Its result is still pending; earlier low-effort results remain failures.


A fresh browser check exposed a second registry loading state: the glossary
was correctly excluded, but visible navigation/footer text arrived before the
empty application main was populated. Generic browser settling now recognizes
empty app/main regions, waits within the existing bound and refuses to use
navigation in place of a still-unloaded app. A real browser recheck captured
19,379 characters including the full trial title, ER+/HER2- population and
No Results Posted. Artifact: registry-hydrated-content-check.json. The official
API endpoint was also readable; no API content was silently aliased to HTML.

The medium recall run read the correct old version, then emitted a mismatched
closing delimiter in knowledge_commit. It remains unaccepted. Plain JSON syntax
diagnostics now name a literal first tool field only to select its frozen repair
schema, and distinguish mismatched nesting from string escaping. No arguments
are repaired or executed. Retry delivery now includes the same current work
observations as ordinary tool results, so missing execution receipts remain
visible after protocol errors.

184 browser/acquisition/research tests and seven subtests passed; 48 targeted
protocol/entry/recovery tests passed. Ruff, mypy and ty passed. Pyrefly identified
one implicit Any in the new DOM boolean check; an explicit object annotation
fixed it and its rerun passed with unchanged existing warnings/suppressions.


## Exact premise feedback and constrained-text diagnostic

The eighth recall attempt (thinking/medium) again reached a successful read but
failed on malformed commit nesting and max_turns. The ninth attempt requested
KoboldCpp response_format=json_schema for only the tool/args envelope, with
thinking disabled. Syntax errors disappeared, but a mistyped premise identity
and a scope without an owner still prevented saving. It remains a failed run;
constrained decoding is not proof of semantic correctness.

Missing premise references now receive the same bounded, scope-filtered exact
identity suggestions as missing reads, before any knowledge writes. Candidates
are never chosen or read automatically. Same-batch references and idempotent
replay retain the repository validation path. Regression coverage includes
private candidate filtering, no partial commit, and explicit resubmission.
26 focused knowledge/repository/lifecycle tests passed; Ruff, mypy, Pyrefly and
ty passed (existing warnings/suppressions unchanged). Commit eb367245.

The manual probe adds --constrained-text as an explicit diagnostic only. It
constrains neither tool names nor argument contents, disables the constraint
for auxiliary compaction requests, and rejects mixing it with native/thinking
modes. It has not yet been integrated as a production setting. Revision
acceptance now snapshots candidates before execution and requires the same
identity, exactly one revision increment, unchanged content/references and
candidate status, in addition to the requested condition/provenance update.
The tenth fresh recall process has actually committed a candidate and read the
correct original claim; its final completion and revision are still pending.


## Controlled lifecycle result and production JSON compatibility mode

Fresh constrained recall completed at turn 7; its premise read happened after
the candidate commit, so this is persistence/lifecycle acceptance, not proof of
ideal research ordering or careful analogy quality. Separate-process revision
completed at turn 5. Manual inspection confirms the same inference identity,
revision 1 -> 2, unchanged content/references and personal owner scope, candidate
status, workload not_extracted and analogy_requires_queue_measurement. The
scoped revision graph contains seven nodes and seven edges. Artifact:
research-three-stage-thinking-clean-20261030/lifecycle-inspection.json.

The JSON option now uses the production provider and shared task freeze path,
not a harness-only payload patch. Local Tool calling has an explicit
JSON constrained (thinking off) mode, persisted as native_tools_mode=json. It
uses Chat Completions response_format=json_schema only for task sends; ordinary
chat sends and auxiliary summaries remain prose. Frozen API selections retain
the format and frozen provider invocations retain the per-send flag. Native,
Responses and thinking combinations are rejected. This constrains syntax only;
it neither selects tools/arguments nor adds permissions or changes validation.
Unsupported server formats surface their actual rejection without switching
protocols or replaying uncertain generations. Plain remains the default.

Four new tests cover config validation, selection restore after changed defaults,
ordinary/auxiliary isolation and real headless/kernel request admission. Expanded
provider lifecycle/entry checks passed 58 tests. Local config/model/cold-start
checks passed 61 after updating the expected allowed-mode diagnostic. Ruff,
mypy, Pyrefly and ty passed; frontend typecheck/build and dist validation passed.

The first real WAL JSON run searched/opened official and secondary pages and
saved three sourced claims, but failed at max_turns after repeating invalid
report citations and a legacy note identity. It is NOT accepted. Its subsequent
investigation also failed to extract useful new evidence. The run exposed
comma-group citations [1, 2] being ignored. The shared scanner and finalizer
now expand explicit lists without selecting or supplying references; unknown
numbers still fail, and ranges/array indexes are not inferred. Feedback now
explains that evidence paragraphs each need inline citations and offers explicit
read requests for supplied legacy references. Strict research snapshots include
a complete report-shape example. 212 focused research/prompt/report tests and
seven subtests passed; no full pytest run. A pre-existing stale done-repair
prompt hash was verified identical between HEAD and working code and updated
to the reviewed explicit nesting-repair surface.

The next live medicine task uses the production JSON mode and these citation
repairs. Its result and a repaired WAL rerun remain pending.


## Copyable saved-version receipts

knowledge_write now distinguishes the database note identity, logical export path
and structured object versions. Successful receipts include a bounded, copyable
knowledge_refs fragment for actual claim/inference/question mutations; it does
not select which versions an answer used. Focused knowledge/lifecycle/repository
tests passed 26; Ruff, mypy, Pyrefly and ty passed. The live medicine process
started before this change and remains unaccepted.


## Live failure: misleading compacted progress and footer-only pages

The production JSON medicine run repeatedly proposed reports before saving
evidence, invented/spliced excerpts and overgeneralized animal findings. A
compacted work-state incorrectly claimed a rejected write had succeeded. This
is a real failure, not accepted research. Continuations now include bounded
research observations derived from existing execution receipts: exact opened
source identities, actual saved counts/findings and recent failed actions. The
same composition is used for ordinary results and completion/protocol repairs,
with no new state ledger or execution loop. Auxiliary summary instructions
explicitly distinguish proposed calls from successful receipts and preserve
evidence scope/uncertainty. Ordinary task prompts remain unchanged.

The same run also accepted a PMC response containing only HHS Vulnerability
Disclosure as an opened article. Acquisition now rejects pages consisting only
of known navigation/footer lines in both browser and HTTP paths, while retaining
articles that discuss those terms. No source body is manufactured or aliased.
82 focused browser, prompt, compaction and lifecycle tests passed; Ruff, mypy,
Pyrefly and ty passed (existing warnings unchanged). Fresh live runs are pending.


The first production-JSON medicine run ended no_progress (zero saved notes).
Its evidence-only investigation inherited report-shaped done repair examples
after a protocol rejection, encouraging irrelevant report rewrites. Ordinary
done repair now preserves the requested task format; the structured example
is selected only when supplied by the frozen task contract. Evidence follow-up
turns also receive the receipt-based research observations without enabling
strict final-report checks. No policy grants or completion checks were relaxed.
31 targeted prompt/contract/observation tests and all four static checks passed;
only the reviewed ordinary done-repair hash changed. A fresh WAL live run is
in progress and has actually searched/opened official documentation, saved
evidence and read its saved note; final quality remains unaccepted.


The repaired WAL live run saved a real official-source claim at turn 3, but
repeatedly reused its document id in done.knowledge_refs after reading the note.
It is unaccepted and was cancelled through the durable request_cancel barrier
for a fresh diagnostic run; no process was killed or uncertain call replayed.
Legacy note reads now lead with a bounded id/revision knowledge_refs fragment
for accessible underlying claims/inferences/questions, before the document
front matter. This is a candidate list, never an automatic choice. The runtime
observation renderer now uses the lossless research_observation.evidence receipt
for claim/stance; the smaller evidence_items projection contains only URL/quote.
181 focused knowledge/research tests and seven subtests passed, plus all four
static checks. Further live acceptance remains pending.


The receipt-guided medicine run successfully saved two notes after opening PMC
and the hydrated trial registry. It then repeated an invalid document id in
knowledge_refs and was stopped via the durable cancellation barrier; it is NOT
a passing report. Feedback now addresses this prerequisite alone when evidence
already exists, with one exact read action; other completion checks still run
on resubmission. Missing-evidence feedback still prioritizes obtaining a real
save. 168 focused tests plus seven subtests passed; the expanded continuation,
subprocess restart, recovered-result and compaction boundary suite passed 58.
All four static checks passed. A clean three-process JSON lifecycle probe is
now running before another fresh real-web attempt.

## Chinese retrieval failure and derived index repair

The clean JSON lifecycle in `artifacts/research-three-stage-json-clean-20261030`
passed source saving and read-before-commit candidate creation. Revision failed:
the model revised a related reader claim instead of the queue inference. The
validator rejected the result despite the mechanical task reporting done. The
original failed store is retained unchanged as a diagnostic artifact.

The revision prompt's background did not contain the target inference: unicode61
does not segment Chinese runs, while incidental English schema terms recalled
long source documents. Structured FTS now adds derived overlapping CJK bigrams,
weights title/alias lexical matches, and automatically rebuilds outdated derived
indexes on open. No aliases, object identities, revisions or commit receipts are
rewritten. Oversized background entries no longer hide later fitting entries.
Task guidance requires searching the requested topic/kind if background lacks it;
a related premise must not be substituted for the target inference.

Regression coverage includes the actual long revision query, English distractors,
scope filtering before limit, existing-index upgrades preserving receipts, and
bounded background rendering. 38 focused tests passed. mypy, Pyrefly and ty
passed; Ruff's new test import ordering was fixed. Only the explicitly reviewed
research-guidance digest changes; ordinary prompt baselines passed unchanged.
The strengthened manual validator requires premise reads before candidate
creation and preserves scope/origin during revision. A fresh same-config lifecycle
is running in `artifacts/research-three-stage-json-cjk-20261030`; not yet accepted.

That run exposed another real failure: the model paired the single-writer claim
with the reader-concurrency quote, and then alternated valid structured reports
with heading-bearing summaries. The completion follow-up itself supplied a
heading-bearing summary example, contradicting the submitted report_sections
format. The failed diagnostic was stopped through request_cancel and retained.

The shared renderer now preserves the submitted format explicitly, using an
argument supplied by the existing control path, without research-specific
branching. Text-binding repair requests one cited finding per paragraph using
saved wording, rather than adding unsupported elaboration. Receipt guidance
also asks for distinctive in-source queries and forbids reusing an unrelated
quote for a different finding. These remain instructions, not a semantic proof
of evidence entailment. 76 focused prompt/report/completion tests and Ruff,
mypy, Pyrefly and ty passed. A fresh real browser WAL task is running in
`artifacts/research-live-wal-focused-20261030`; acceptance remains pending.

The focused WAL run fixed its invalid note reference after a real knowledge_read,
but still drafted a broad report after saving just one English concurrency
finding. It omitted the long-reader/checkpoint limitation and suggested that
BEGIN EXCLUSIVE reduces write conflict. The gate rejected unbound statements;
the model rewrote instead of collecting the missing evidence. The diagnostic
was cancelled through the durable barrier and is not a passing report.

Research methods now explicitly require each requested comparison/constraint to
have a saved direct finding or a named gap. Reusable claim sentences use the
requested answer language, with original-language excerpts unchanged. Strict
report snapshots show only the preferred structured example; ordinary done
examples and the alternative Markdown interface remain supported unchanged.
53 focused lifecycle/prompt/report tests passed and all four static checks
passed after fixing a test helper style issue. These changes are guidance, not
a guarantee of model compliance or semantic entailment.

The medicine task in `artifacts/research-live-adaptive-focused-20261030` opened
two PMC papers and saved an original-language excerpt with a Chinese mechanism
claim. It nevertheless proposed efficacy statements and cited the unopened
trial registry, repeatedly rewriting the report instead of acquiring evidence.
It is unaccepted and was stopped via request_cancel. Completion feedback now
prioritizes cited unread sources, then opened-but-unsaved citations, ahead of
wording/formatting checks. It supplies copyable open_url actions only when web
reading is authorized, respects observed redirect aliases, and creates no
evidence or execution state. Remaining checks rerun on the next proposal.
34 focused source/report/completion tests and all four static checks passed.

## Same-configuration lifecycle passed

`artifacts/research-three-stage-json-prerequisites-20261030` completed all three
independent processes using production JSON mode, thinking off, temperature
0.3 and seed 73: first 7 turns, recall 4 turns, revise 4 turns. Both first-run
claims have their corresponding official English excerpts, WAL/workload
conditions and requested alias. Recall read the writer claim before committing
the candidate inference. Revision searched/read that exact candidate and changed
it from v1 to v2, preserving identity/content/references/origin/scope/status while
adding the requested unknown workload and analogy interpretation. The manual
validator exited 0. The association answer still contains untested explanatory
elaboration; this pass establishes persistence/recall/revision, not verified queue
performance. Expanded focused source/lifecycle/report/prompt coverage passed 106.

Inspection also found repeated identical page acquisition changed only the
source's retrieved_at but created another knowledge version, unnecessarily
marking earlier dependencies for review. Identical snapshots now retain their
first knowledge observation time/version; later acquisitions retain their own
execution/ledger observations. Changed content still creates a separate snapshot.
28 focused archive/repository/lifecycle tests and all four static checks passed.
The next real medicine task is `artifacts/research-live-adaptive-prerequisites-20261030`.
