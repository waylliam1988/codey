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

## Grammar and continuation diagnostics after the lifecycle pass

The medicine-prerequisites run was cancelled and remains unaccepted: after one
mechanism finding it duplicated a full report in summary/report_sections and
described a registry with no posted results as preliminary efficacy. The
source-prerequisite feedback cannot repair a proposal rejected earlier by
protocol validation. Browser acquisition itself succeeded.

`research-live-wal-thinking-low-20261030` failed before its first tool call:
the recorded HTTP response repeated channel/thought template markers until the
5000-token limit. This is malformed generation, not useful extended thinking or
a browser timeout. No malformed text was promoted to an executable action.

Two richer grammar experiments are retained as failures:
`research-live-wal-contract-20261030` generated args before tool, selected the
wrong same-shaped tool and repeated empty knowledge searches; the normal
no-progress barrier stopped it. `research-live-wal-ordered-contract-20261030`
selected tool first but repeatedly omitted evidence. KoboldCpp's compiled
argument grammar orders required fields first, then optional fields: choosing
sources can make a later evidence property unavailable. It was durably cancelled.
Full parameter grammars are therefore NOT shipped.

The production optional JSON mode now freezes the current tool-name enum in
the existing provider request and uses ordered allOf components to select tool
before its unrestricted args object. Arguments remain validated/authorized by
the same kernel. This avoids sampler-imposed parameter order and preserves
ordinary prose, auxiliary summarization, Markdown-only reports and native mode.
The request schema is invocation data, so existing durable preparation/recovery
owns it; no provider-global mutable schema or second execution path is added.

`research-live-wal-ordered-envelope-20261030` confirmed successful evidence
saving again but was cancelled after repeated broad reports with only one
English finding. It is not a quality pass. Research continuation guidance now
also gives a concise Chinese reminder when the task explicitly requests 中文:
save Chinese finding sentences with original-language excerpts, investigate
each requested comparison, and do not duplicate a report in summary. Permission
and evidence-only boundaries remain intact. This is guidance, not entailment
validation. Next live diagnostic: `research-live-wal-localized-20261030`.
73 focused JSON/protocol/observation/prepared-request/restart tests passed.
Ruff, mypy (553 files), Pyrefly and ty passed. JSON Schema validation in tests
uses jsonschema, now explicitly listed in the development dependencies.

## Evidence-label and report-contract fixes

`research-live-wal-localized-20261030` saved several Chinese claims, but paired
one long-reader claim with a table-of-contents heading and added a single-writer
claim to a quote about appending transactions. It also reversed the official
"slower" read-heavy observation into "faster". Cancelled; not accepted.

The ledger now rejects an exact page title or Markdown section label as evidence
for a finding (source-catalogue notes can still quote titles). Whole batches
remain atomic. This recognizes labels only; it does not prove entailment or
detect every wrong quote. Truncated HTML rendering adds at most 16 actual
section offsets, inside the untrusted-source block. These are locators, not
invented findings; the source and offset identity remain unchanged.

The strict Research done description now has one consistent rule: structured
summary is conclusion-only; Markdown-only complete reports remain supported.
Ordinary done is unchanged. The sourced-note example no longer teaches an
arbitrary expiry date, confidence number or unrelated conceptual relation.
Chinese continuation asks to archive one direct finding at a time.

`research-live-adaptive-localized-20261030` opened real PMC sources and saved a
mechanism finding after repairing the conditions map. It used a correctly
separated conclusion/report_sections, but still overstated p=0.065 as significant,
proposed a reconstructed quote (rejected by exact-source validation), and did
not compare primary reported human results. Cancelled; not accepted.

Verification: 187 focused research/prompt/recovery tests plus seven subtests;
15 source-rendering/locator tests; 61 contract/ledger/rendering tests plus two
subtests passed. A combined 48-test prompt/label/lifecycle/JSON check also
passed. Ruff, mypy, Pyrefly and ty passed. No full pytest was run.

Next diagnostic: `research-live-wal-clock-turn-20261030`. The manual harness
offers explicit --merge-clock to test whether a separate trailing clock-only
user turn harms instruction following in this template. This is an experiment,
not production behavior or acceptance; all payloads and the flag are recorded.

The clock-turn experiment ended at no_progress, repeatedly translating a
traditional-Chinese source into simplified Chinese and then English inside
excerpt. It did not establish a clock-boundary fix; production clock handling
is unchanged. The original requests/results remain preserved.

Rejected-excerpt feedback now says not to translate or convert scripts. If an
already-opened source has a sufficiently similar short paragraph, the rejection
shows that exact original passage and its offset/page as an inspection hint.
It does not replace the submitted quote, save an evidence item, or declare the
claim supported. Matching is bounded (256 paragraphs, <=1200 characters per
candidate, similarity >=0.65); unrelated proposals receive no hint. Tests verify
rejection atomicity and distinguish subsequent explicit successful resubmission.
161 focused ledger/research/recovery tests plus seven subtests and all four
static checks passed. Next production-configuration run (no clock experiment):
`research-live-wal-source-repair-20261030`.

## Latest-result focus diagnostic

The source-repair run was durably cancelled and is NOT accepted. It saved a
false SQLITE_BUSY explanation using a real but unrelated concurrency quote,
omitted requested version/workload coverage, and repeated a counter-section
rejection. Exact quote validation cannot establish entailment. Its artifacts,
including the bad saved finding, are retained only in the isolated diagnostic
store; they are not accepted research knowledge.

Payload inspection disproved the suspected initial double tool contract: the
initial writer leaf does not add another wrapper. Full contracts do recur in
later historical user turns. No substring-based removal was introduced.

The shared delivery prompt now places historical execution observations before
the latest result; control repair likewise leaves the specific rejection last.
Source text keeps its untrusted-source framing and receipts/authorization are
unchanged. This avoids burying a new passage or repair instruction under several
thousand characters of old findings. The production-configuration browser task
`artifacts/research-live-wal-result-focus-20261030` is in progress, not accepted.

That run subsequently searched after a rejected draft, but still skipped the
requested version comparison. Follow-up used a search-only quote from a page
returning HTTP 410 (write rejected), reused the concurrency claim for a different
finding, and generated malformed report data. Durably cancelled; not accepted.

JSON task contracts now travel as frozen per-request declarations through the
existing prepared invocation, like the JSON tool-name schema. The complete
contract is counted in each admitted HTTP payload but does not accumulate as
obsolete catalogs in canonical conversation history or auxiliary summaries.
Plain text and native tool paths retain their contracts. JSON task prompts no
longer offer ordinary prose as an alternative to the required tool envelope.
Tests cover exact prepared-request restoration, counting, nonmutation of prior
history, and no contract leakage to later ordinary/auxiliary sends. This is a
context-cost fix, not semantic validation. Fresh browser research is required.

Validation: 83 focused protocol/prompt/prepared-request/delivery tests passed;
Ruff, mypy (553 files), Pyrefly and ty passed. No full pytest was run.

The result-focus cancellation also exposed a system defect: a stopped shared
investigation could be interpreted as no new material and the pipeline returned
its previous accepted draft with stop_reason=done even while task_status was
cancelled. Investigation now propagates its stopped outcome as TaskCancelled.
An actual durable-cancellation regression exercises the production kernel and
ensures no previous draft can be returned as successful completion. The related
45 pipeline/follow-up/restart tests passed; all four static checks passed.

`research-live-wal-scoped-contract-20261030` confirms one tool catalog per
admitted payload instead of repeated historical catalogs, but still is NOT a
semantic pass. It translated "Multiple transactions can be appended" as
simultaneous appending, skipped direct checkpoint/BUSY evidence, and repeated
rejected drafts. Durable cancellation requested; preserve this isolated store.

Inspection found contradictory binding-repair guidance: after listing missing
findings, its unconditional final instruction told the model to rewrite them
using existing claims. Repair now prioritizes locating and archiving the missing
fact; only the same predicate/conditions may reuse old wording. It preserves
authorization, rejected report content, and all completion checks. Eighteen
focused completion/repair/observation tests and all four static checks passed.

The evidence preparation path also clipped claim content to 260 characters
before building long-term objects. This could discard a trailing condition or
negation. Prepared evidence, database objects and lossless recovery receipts now
retain the full normalized claim; the public/UI projection still applies the
260-character display bound. A regression verifies the trailing applicability
limit survives receipt replay and closing/reopening the database. Ninety-two
focused lifecycle/ledger/recovery/observation tests and all four static checks
passed. This prevents information loss; it does not validate claim semantics.

Next real medical browser run:
`artifacts/research-live-adaptive-scoped-repair-20261030`. In progress; not
accepted. It has opened original studies but again proposed a significant
capecitabine-alone effect despite a nonsignificant reported comparison.

The medical scoped-repair run was cancelled and remains unaccepted: it saved
an overstated significance claim and made unverified clinical extrapolations.
Three short-context diagnostic calls on the exact original result passage are
retained in `artifacts/research-short-comprehension-20261030`: plain/thinking-off,
JSON/thinking-off and plain/thinking-on all preserved the nonsignificant
capecitabine-alone comparison. This isolates an adverse long-task/context effect;
it does not establish that short-context outputs are always correct.

A conservative support-qualifier check now rejects an affirmative significant
effect when the quoted passage explicitly reports nonsignificance and the claim
drops that distinction. Mixed comparisons must be split or retain the limit.
Counterevidence stances remain valid. It does not choose a p-value threshold,
check other statistical reasoning or establish general entailment. Whole
evidence batches still reject atomically. Related validation: 64 focused tests
before the final pattern expansion, then all 17 qualifier cases passed; all four
static checks passed (554 production files for mypy).

## Receipt-backed text results and soft admission

The `research-live-adaptive-receipt-views-20261030` real-browser run was
durably cancelled (exit 1, stopped). It opened Nature, PMC, ClinicalTrials.gov
and NCI pages, but confused mouse and human findings and repeated fabricated
quotes. Its saved objects and report proposals are diagnostic artifacts, not
accepted knowledge. The support-qualifier guard does not prevent all incorrect
interpretations, especially fabricated quotes (which exact-match checks reject).

Text JSON tool outputs were previously user messages invisible to deterministic
native-output reduction. The common kernel now freezes trusted output spans,
digests and existing receipt references in prepared invocation arguments. The
provider validates these and omits only backed older bodies from its counted
view. Markers inside source/user text cannot authorize omission. Surrounding
instructions and current source remain intact, including after later repairs.
Internal provenance is stripped from HTTP messages. Original events and receipts
remain lossless; read_tool_result can retrieve omitted bodies.

At 60% of input capacity, two eligible backed results trigger deterministic
reduction within the current admitted exchange. This also works when background
view commits cannot replace durable canonical input; it uses existing staged
checkpoints rather than a second runtime or ledger. No semantic model request
is needed for this reduction.

Validation: 64 focused JSON/delivery/compaction tests passed, three optional
delivery cases skipped. Ruff, mypy (555 source files), Pyrefly and ty passed
(Ruff import ordering corrected and rechecked). Live quality retest pending.

The soft-admission medicine retest actually sent receipt pointers (five older
bodies omitted by turn 13). It corrected the nonsignificant single-drug mouse
comparison after qualifier feedback, but old runtime observation snapshots
accumulated on every turn and the request still reached 84k characters. It
omitted the requested human outcome comparison and repeated spliced quotations.
Durably cancelled; not a quality pass.

Latest runtime observations now travel as frozen request-only arguments in
JSON mode. They are counted in admission, rendered as user-role data immediately
before the current result/repair, and never accumulate in conversation history
or auxiliary summaries. Source spans retain their original offsets; existing
prepared-request recovery restores the original snapshot rather than rebuilding
it from newer facts. Plain/native paths retain their existing behavior.

The fresh scoped-contract lifecycle graph has also been exported and inspected:
`research-three-stage-scoped-contract-20261030/revise-graph.json`, seven nodes,
seven edges, candidate inference at version 2, original premise at version 1.
This is version/provenance acceptance, not proof of the queue analogy.

## Evidence investigation phase contract

The current-state medicine run retained one observation snapshot per request,
but still confused related mechanisms and attempted full reports inside an
evidence-only follow-up. Its process was interrupted during the session/tool
environment transition; no final result artifact exists. KoboldCpp is idle and
no manual-probe process remains. Do not treat it as a completed research task.

A concrete instruction conflict was found: the follow-up embedded the parent's
request for a Chinese research report, its Chinese observation reminder demanded
that report again, and its shared done description still offered report_sections.
Evidence-only snapshots now expose only done.summary with an acquisition receipt
description. Text and native validation reject report_sections in this phase;
parent research retains the full report contract. The Chinese reminder and the
investigation's closing instruction now ask only for actual saved findings and
remaining gaps. No executor, recovery state or completion check was bypassed.

Validation: 66 focused snapshot/protocol/observation/follow-up/subprocess-restart
tests passed. Ruff, mypy (555 files), Pyrefly and ty passed. Fresh real-browser
WAL task: `research-live-wal-phase-contract-20261030`, in progress, not accepted.

The phase-contract WAL run has now ended with `done`, but is NOT accepted:
it omitted old/current version differences and concrete workload measurement
limits, used secondary blogs for several requested official-source findings,
and the follow-up immediately repeated the parent without acquiring anything.
The new summary-only done contract did prevent another full report loop.

The investigation prompt now supplies bounded ledger-backed parent findings
and opened URLs instead of the entire provisional report. Its method guidance
does not request forbidden done.knowledge_refs. Pure research observations no
longer repeat the complete task/report and empty coding verification fields.
An acquisition-enabled investigation must have a real tool-result receipt
before ending; failed acquisition can honestly leave gaps. This check does not
establish semantic coverage, and extraction-only legacy phases are unaffected.

Validation: 105 focused tests passed, including subprocess restart, follow-up,
protocol and observation checks. Ruff, mypy (555 files), Pyrefly and ty passed.
Fresh live WAL retest: `research-live-wal-targeted-investigation-20261030`,
still running and not accepted.

## Parent synthesis after investigation

The targeted-investigation WAL run ended with the old draft: acquisition now
really searched/opened passages, but several proposed quotes were rejected.
The pipeline had no gated parent synthesis after acquisition. It only merged
an ungated evidence patch, which must remain partial and could lose ranking
against the old draft simply because the draft had stopped with done.

Production now retains validated acquired findings and passes them to a fresh
parent synthesis through the same driver and continuation (attempts 1 initial,
2/4/6 acquisition, then 3/5/7 final synthesis). Sources/evidence are inherited
from the ledger, output is checked by the common completion gate, and a failed
or cancelled synthesis cannot fall back to the older successful draft. The
bounded handoff includes source identity, stance, exact excerpts and note ids;
long displays are explicitly marked incomplete. Stable synthesis identities
are distinct per invocation, with no duplicate final persistence projection.

Validation: 52 focused pipeline/source/restart tests passed; subprocess crashes
now also cover the final synthesis response and settled output. An additional
28 focused patch/phase/handoff tests passed (groups overlap). Ruff, mypy (556
files), Pyrefly and ty passed.

`research-live-wal-parent-synthesis-20261030` completed acquisition and the new
parent synthesis with done. It is still NOT a semantic pass: the report omits
requested version distinctions and understates long-reader checkpoint limits.
No claim of broad research reliability follows from this completed runtime.

## Source locator ranking and reading guidance

Actual source_search for `checkpoint and SQLITE_BUSY` returned three overlapping
TOC previews. Common query words no longer dominate multiword search; body
matches rank above heading/TOC matches, and near-duplicate windows no longer
fill the result budget. Labels remain searchable when no body match exists.
Page offsets and tool-receipt offsets now have distinct navigation guidance;
source tool results no longer generically direct every next read to a receipt.
45 focused source/protocol/compaction tests passed (overlaps earlier groups).

Next live run: `research-live-adaptive-parent-synthesis-thinking-20261030`,
real production browser, local Gemma4 with thinking/minimal enabled. In progress,
not accepted. Normal text protocol is used because the JSON-only diagnostic
sampler excludes thinking. This is a configuration comparison, not a silent
change to the user's model preferences.

## Shared request context across API protocols

The thinking/plain medicine run was durably stopped after detecting six old
research snapshots in one request (76,546 characters). Request-only tool
contracts, current observations and receipt-backed text views now cover plain
text as well as JSON mode; native API turns also receive a single ephemeral
observation. JSON grammars remain exclusive to JSON mode. Prepared request
recovery still uses the frozen original arguments, and native terminal
acknowledgements do not create another generation.

Validation: 71 focused protocol/delivery/prepared-request tests passed, three
optional skips; after expanding plain/JSON soft-admission coverage, 22 focused
tests passed. Ruff, mypy (556 files), Pyrefly and ty passed (test groups overlap).

Live retest: research-live-adaptive-all-protocol-context-20261030 confirmed one
current observation and one contract per plain request. It searched and opened
MDPI, ClinicalTrials.gov and PMC and persisted findings. It then repeated done
with source entries [2]/[3] lacking URLs. Feedback mixed citation-table repair
with unnecessary investigation suggestions. It also promoted a registry's
"may slow" into an unqualified benefit and omitted reported human outcomes.
Durable cancellation requested; this run is NOT a semantic quality pass.

## Citation repair priority and explicit uncertainty

The preceding run exited stopped after durable cancellation. Missing numbered
URL entries now take precedence over downstream claim-binding feedback. Repair
shows actual opened URL/title candidates without guessing their citation
numbers, and asks for mapping repair only; all evidence checks rerun afterwards.

Evidence writes also reject a narrow explicit may/might-effect qualifier loss
in supporting claims. Hypothesis/aim wording is allowed, calendar May is not
treated as uncertainty, and counterevidence retains its stance. This is not a
general entailment check; an exact quote can still be misinterpreted.

Lexical proof hints now retain post-question comparison/check clauses instead
of discarding all text after the question mark. Delivery/storage directives
remain excluded. This does not establish semantic coverage.

Validation: 50 focused evidence/report/completion tests and 19 proof tests
passed. Production Ruff, mypy (556 files), Pyrefly and ty passed.
Real-browser retest: research-live-adaptive-citation-uncertainty-20261030,
thinking/minimal, currently in progress; no quality acceptance yet.

## Child receipt guidance and draft self-recall

That retest ended with protocol failure in final synthesis, not an accepted
report. Acquisition really used source_search/open_hit and saved another
finding, but still missed human outcomes and conflated study arms. A child
knowledge_write receipt also told it to supply forbidden done.knowledge_refs.
The tool facade now receives per-call evidence-only presentation on its copy;
write/read/search receipts retain identities and versions without instructing
that child to fill the parent's answer-reference field. Parent guidance remains.

More significantly, fresh parent synthesis automatically recalled its own saved
initial answer. Its request thus contained an old report despite the bounded
evidence handoff. Automatic background now excludes answer objects in SQL before
seed limits and relationship expansion. Explicit search/read still exposes
historical reports; automatic background uses underlying knowledge instead.

Removed the copyable generic "no strong counterevidence" example from both
completion guidance and tool examples. Guidance asks for actual investigated
limits/search scope or an honest not-investigated gap. Methods also request
independent comparator searches and separate multi-arm findings.

Validation: 80 focused knowledge/context/phase tests passed, including 30 old
reports competing with a usable premise; 78 earlier focused method/phase tests
passed (overlap). Ruff, mypy, Pyrefly and ty passed before the final example-only
edit. Broad semantic acceptance remains outstanding.

`research-live-adaptive-isolated-synthesis-json-20261030` repeatedly emitted
empty/undeclared keys during knowledge writes (including an empty stance key)
and reused ellipsized excerpts. Cancelled as a failed protocol diagnostic; no
semantic pass. JSON constraints did not resolve this local model's argument
quality. Unknown-property repairs now explicitly require changing/removing the
rejected key while preserving valid source strings. Final synthesis explicitly
audits saved model extractions and may correct a note, rather than treating
persistence success as interpretation approval. Nineteen focused repair/quote
tests and ten synthesis/observation tests passed (overlap); all static checks
passed. Sixty pipeline/final-synthesis/subprocess-restart/protocol tests also
passed after the preceding self-recall and receipt changes.

## Query-local locators and missing update identity

`research-live-wal-native-isolated-20261030` ended protocol-failed. It really
searched/opened SQLite.org and located passages, but repeated locator queries,
created duplicate notes under a guessed update id, then emitted incomplete
native done frames. It is not accepted.

Two concrete defects were corrected: locator footers previously suggested the
first historical rN/hN rather than the current query's first match; and an
explicit unknown knowledge_write.id silently fell back to creating a new note.
Receipts now retain current targets even when ids are reused. Displays use only
current-query locators, and empty/failed searches do not show historical hits.
Unknown explicit update ids fail without writes; new notes omit id. Schema and
receipt guidance distinguish note updates from structured object ids, and
bounded observations retain evidence note_id for later inspection.

54 focused knowledge/source/observation/report tests and 52 source/feedback tests
passed (overlap). Ruff, mypy (556 files), Pyrefly and ty passed. Fresh final-code
three-process lifecycle: research-three-stage-final-isolated-20261030, in progress.

## Focused report repair and controlled lifecycle retry

That lifecycle was cancelled and exited stopped; it did not complete three
stages. Its short saved claim fragments and uncited counter section triggered
both citation and lexical binding feedback. Report-quality defects now receive
one focused repair before binding hints, provided the report sections exist;
all evidence checks run again on the repaired draft. Missing-section/literal
newline repair keeps its existing priority.

The manual lifecycle fixture now explicitly requests full Chinese claim
sentences, separate cited conclusion paragraphs reusing those sentences, and a
citation on the single-writer limit. This is a more explicit controlled fixture,
not evidence of improved broad research quality. A fresh directory is required.

Validation: 26 focused completion/structured-report tests and 44 completion/
lifecycle/durable-continuation tests passed (overlap). Ruff, mypy (556 files),
Pyrefly and ty passed. Broad real-web semantic acceptance remains outstanding.

## Honest incomplete investigation and current lifecycle result

The Chinese current-observation reminder still requested an investigation for
every unmet feedback item, contradicting focused citation/format-only repairs.
It now distinguishes those repairs from actual missing-evidence acquisition.
The report format gate also permits explicit unfinished counter-search
declarations. The structural proof review does not count such a declaration as
counterevidence_checked. Uncited factual counterclaims remain rejected. This is
explicit-gap recognition, not a semantic evaluation of the completed search.

The focused three-process suite at
artifacts/research-three-stage-focused-final-20261030 exited zero for all stages.
It searched/opened frozen official pages, persisted claims with unknown workload,
read an earlier version in a different process, saved a candidate analogy and
revised that same inference from revision 1 to 2 with pinned premises intact.
This validates the controlled lifecycle. Manual inspection found over-compound
initial claims and an incomplete final revision explanation despite valid JSON.
It is not broad semantic acceptance, and the fixture changes above are material.

Expanded pipeline tests exposed two creation fixtures still supplying a guessed
update id. They now create without id and use the returned identity for updates,
consistent with the deliberate unknown-update rejection; ownership/provenance
assertions remain intact.

36 focused report/counter tests passed, followed by 216 expanded research tests
and seven subtests (overlap).
Ruff, mypy (557 files), Pyrefly and ty passed. A real web WAL probe with explicit
thinking/low is isolated in research-live-wal-focused-feedback-low-20261030;
no default provider configuration was changed and quality is not accepted yet.

## Browser retests and sufficient excerpt context

The thinking/low WAL probe produced only repeated channel/thought template
markers before any tool call. Read-only KoboldCpp pending-output inspection
confirmed the loop; its raw output is saved as template-loop-partial.json.
After durable cancellation the owned local generation was aborted and the
process exited stopped. This is a failed model/template diagnostic, not a
browser failure.

The following thinking/minimal plain run really searched/opened SQLite.org,
saved concurrency and single-writer passages, and used source_search/open_hit
after ellipsized evidence was rejected. However it eventually shortened the
tradeoff quote to a generic introductory sentence while keeping a detailed
claim, then repeatedly emitted malformed final frames (nested summary and
missing fields). It was cancelled, exited stopped, and is not accepted.

The current-observation instruction unnecessarily required one short sentence.
It now requests a contiguous passage sufficient for the whole claim, explicitly
allowing adjacent sentences and requiring separate findings for separate
passages. The locator/exact-quote checks remain strict. Eleven focused
observation/API-context tests passed. A fresh JSON/temperature=0.7 real-web
diagnostic is isolated in research-live-wal-json-passage-20261030; defaults are
unchanged and semantic review is pending.

## Current-run references and source-discovery isolation

That JSON/0.7 run ended no_progress after repeating a misspelled claim id. It
acquired and saved evidence from a secondary Chinese explanation, but never
opened the requested official source. It is a failed broad-quality diagnostic.

The existing research synthesis archive already attaches exact versions from
the run's newly saved notes. Parent research write receipts/method guidance now
explain that this does not require manually copying those long ids into done.
knowledge_refs. Previously recalled knowledge still needs explicit pinned refs;
explicit refs remain fully validated. Evidence-only receipts retain their prior
contract. This is per-execution presentation, never a global facade mutation.
A regression checks the resulting answer references actually contain the saved
claim/evidence/source versions without any model-supplied refs.

82 focused lifecycle/phase/task-entry tests passed; Ruff, mypy (557 files),
Pyrefly and ty passed. Restart/production-entry checks are running separately.

The fresh sqlite_wal_official_zh.txt fixture preserves the broad WAL questions
and adds an official starting URL, without supplying the answers. This isolates
source-discovery difficulty from page reading/comparison; it cannot substitute
for the original unseeded source-discovery benchmark. The real-web probe is in
research-live-wal-official-auto-refs-20261030 using JSON/temperature=0.3.

## Current-run recall isolation and quote repair visibility

That official-URL-seeded probe finished mechanically, but FAILED semantic review:
its report incorrectly claimed multiple simultaneous WAL writers could commit,
misreading sequentially appended transactions as concurrent writes. It also
missed requested BUSY/version boundaries. The isolated artifact is not accepted
knowledge and a successful completion gate is not semantic approval.

The subsequent narrower concurrency probe (research-live-wal-concurrency-final-
20261030) really searched/opened SQLite.org, saved evidence, investigated and
entered final synthesis. It exhausted its final 18 turns on repeated full-report
format/citation repairs. It still asserted unsupported simultaneous appends.
It is also FAILED, not a reliable research report.

Automatic background recall now excludes this run's saved non-entity objects
before seed and graph-neighbor limits, retaining capacity for genuinely earlier
knowledge. Explicit search/read remains available. A regression checks 30 new
objects cannot crowd out an older premise or return through incoming/outgoing
edges. Quote rejection now leads with actionable source_search/offset repair
instructions: long URLs and rejected text can no longer push the instructions
past the 400-character observation boundary. Failed quotes are never silently
replaced or persisted. The prior 38 recall/store tests and 163 quote/observation
tests plus seven subtests passed (overlap with subsequent validation).

## One strict report interface (cold-start cutover)

At the user's explicit request there is no legacy strict-research completion
interface: done requires summary (conclusion only) and the five report_sections
arrays. Native schemas, text contracts and admission agree. Plain final prose
cannot bypass the required fields. Evidence-only done still accepts just its
acquisition receipt; ordinary tasks retain their own answer contract. Markdown
is the internally rendered report, not an alternative model input. Removed the
alternative from duplicate-report errors, completion guidance and format repair.
Tests now submit canonical arguments rather than retaining a product adapter.

The structured-only broad live probe really searched/read secondary and official
pages, but repeated an uncited counter section despite feedback. It was durably
cancelled; abort surfaced OutputLengthError/error while durable task_status is
cancelled. It is NOT accepted. Repair feedback now names report_sections.counter,
quotes the failing section and explains the local repair in Chinese; it never
inserts a citation or invents a negative search. Reading this run's own saved note
no longer reintroduces manual knowledge_refs instructions. Earlier notes still
expose precise reference candidates.

Validation: 300 focused/expanded task-entry, pipeline, ledger-recovery and
lifecycle tests plus seven subtests passed. After field-local repair, 52 focused
report/lifecycle tests passed (overlap). Ruff, mypy (557 production files), Pyrefly
and ty passed. The narrower real-web concurrency retest is isolated in
research-live-wal-field-repair-20261030; it is a diagnostic, not a substitute for
the broad WAL or medical task. Semantic acceptance remains outstanding.

## Interpretation identity and review boundary (2026-10-30)

Located model extractions now have origin=model_extraction and status=candidate.
Source-author interpretation is no longer inferred from a successful excerpt
locator. Structured reads, search/background recall, write receipts, synthesis
handoffs and graph details expose interpretation_review=unreviewed. There is no
independent semantic assessment subsystem: answer adoption, a model-authored
provenance flag and unchanged prerequisites cannot create an approval. The
separate dependencies_changed field replaces the ambiguous needs_review field;
there is deliberately no old-field compatibility alias.

A regression deliberately saves the incorrect multiple-writers claim against a
real reader/writer quote, then reopens, recalls and adopts it in an answer. All
views retain the unreviewed identity; automatic promotion is rejected. This
tests honest uncertainty propagation, not automatic detection of the wrong
interpretation. Existing historical diagnostic stores were not rewritten.

Validation: 49 focused knowledge, lifecycle, observation and diagnostic tests;
one frontend graph test and TypeScript; Ruff, full-production-tree mypy (557
files), Pyrefly and ty passed. Pyrefly reports zero errors with 35 suppressed
diagnostics and 77 warnings, not a warning-free result.

Fixed-material local results already exist at
artifacts/research-layered-fixed-local-20261030 (18 serial requests: three cases,
three levels, two seeds). Do not repeat them merely because context was lost.
Manual semantic review and a stronger reference-provider comparison remain
separate tasks; no reference provider has been run. The subsequent live-web
WAL run is artifacts/research-live-wal-unreviewed-20261030. Its done status does
not establish broad question coverage: inspect the report and raw receipts.

## Shared observations and bounded completion repair

The first unreviewed WAL run really searched/opened SQLite.org, saved excerpts,
investigated and returned done. Its final synthesis created three duplicate
finding notes (six finding notes plus the report). The fresh synthesis's current
observation list was empty despite an existing parent ledger; generic guidance
still requested saving before drafting. Current observations now read that same
committed ledger in every kernel transport path. Local recent receipts remain
local: displaying earlier findings does not invent a child execution receipt.

The same seeded broad question was rerun at
artifacts/research-live-wal-shared-observations-20261030, same model/seed and live
browser. It returned done with three finding notes plus the report, and made no
new finding writes in final synthesis. It preserved single-writer serialization.
This is useful mechanical progress, not broad semantic acceptance: it still
omits the requested version boundary, inadequately covers SQLITE_BUSY, and
overstates that reads will necessarily end to allow checkpoint progress. The
query/locator choices also varied, so this is not a controlled causal estimate.

Rejected completion proposals now participate in the shared operation's bounded
progress history. Three identical proposal/rejection pairs stop as no_progress,
preserving the unaccepted draft and feedback. Distinct proposals/feedback can
continue, bounded by the existing turn budget. The history is saved atomically
with the control decision; no synthetic tool effect/batch or second research
counter is introduced. Text/native closure and snapshot roundtrip are tested.

64 focused completion, observation, structured-report and cold-start tests pass.
Expanded testing also passed the research subprocess continuation tests. It
exposed obsolete fixture failures: the cold-start synthesis stub lacked the
SQLite repository API, and an authorization test matched an old error message;
these two tests now exercise current contracts. The broader recovery-trace
fixture initially failed after its direct ProviderActions send because it
omitted the required original invocation scope. A representative failure was
reproduced with all five modified runtime/guidance modules loaded from HEAD
before these changes. The fixture now binds its original snapshot through
sink.bind_request, as production does; all 81 traces pass without relaxing
production recovery. One research restart test run overlapped a source edit and
correctly rejected changed implementation identity; a stable-source rerun passes
all 12 research subprocess continuation tests. The historical parity fixture
still supplies a now-rejected full Markdown done instead of report_sections;
that historical suite is not reported as passing.

The fixed-material semantic review is in research-layered-review-20261030.md.
An unseeded real-web adaptive-therapy task is running separately in
artifacts/research-live-adaptive-unreviewed-20261030. Its results require source
and comparator review; do not infer acceptance from task status.

## Field-local reference repair from a real medical failure

That medical run ended no_progress: it acquired real web pages (mechanism
research, a clinical registration and the mouse preprint), saved candidate
findings and repaired both an uncertainty omission and a nonsignificant-result
overstatement. It did not complete a correct comparison with published human
outcomes. Final synthesis submitted document note ids as knowledge_refs, then
repeatedly read the same note in response to generic reference feedback. This
is a failed broad research test, not an accepted medical report.

Current-run note references now get precise removal instructions: the parent
already attaches those exact saved versions. The rejected proposal is not
silently normalized or accepted, and earlier-work references must remain pinned.
The unconditional upper-gate instruction to read the note was removed so it
cannot contradict the field-local remedy. Ordinary/hybrid reference validation
retains its own read/repair behavior. Note reads also show claim interpretation
state before document prose: an archival note's active status is not semantic
approval of its body.

Final focused verification: 106 tests pass across knowledge storage/review,
lifecycle, structured report, completion repair, shared kernel progress and
native closure. The three type checkers pass over production code, Ruff passes
over codey and the new/updated tests, and git diff --check passes. The frontend
graph test, TypeScript and production build passed; generated codey/web was
published locally and tools.publish_frontend --check passes. No running Codey
service or user's model configuration was restarted/changed by these probes.

The same broad unseeded medical question reran from a fresh database at
artifacts/research-live-adaptive-reference-repair-20261030 with the same local
sampling controls. It passed the initial report/reference path without the
previous read loop and proceeded to actual follow-up browsing. Final status
was done, but semantic acceptance FAILED: it described a trial protocol as
optimizing efficacy, treated P=0.065 as lower significance instead of a
nonsignificant result, and did not recognize the preprint and formal publication
as one study. It also failed to compare published human adaptive-therapy
outcomes as requested. This is mechanical reference-repair progress only.
The process logged a Playwright TargetClosedError during browser shutdown;
the result/report artifacts were produced, but that cleanup exception remains
unresolved. No stronger reference-model run has been performed. The user has
confirmed that only the existing local model is currently available.

## One-finding interface and selected citations (2026-10-30)

Research now exposes save_finding(claim, source_ref, passage_ref), with optional
conditions and an optional explicit comparison. Open tools label exact visible
passages with content-derived references. Source receipt plus character ranges
rebuild those bindings on recovery; source_search previews cannot authorize a
finding. A passage keeps its original source snapshot even after that URL
changes. The existing note/knowledge transaction and common tool effect own
the write; an identical retry reuses the same note and claim version.

Strict research hides the more complex note/commit/link tools. General/hybrid
knowledge revisions retain them. Conditions remain optional and unknown fields
stay unknown. Comparison fields are model assertions, not verified observations.
Every extraction remains candidate/unreviewed.

The sole structured report input has four paragraph arrays (evidence, counter,
source_quality, coverage). It no longer accepts a model-written sources field.
The model cites selected [f...] findings; the application resolves exact pinned
claim versions and generates the source table/numbers and actual query record.
Uncited findings and opened-but-unused pages are not answer premises. An unknown
reference fails with a specific repair rather than fuzzy matching or automatic
addition of supporting evidence. Markdown report output still has six sections.

At this checkpoint 98 focused tests and two subtests pass, including both common-kernel wire
protocols, old-source snapshot selection, forged/cross-source reference refusal,
exact write replay, and explicit answer dependencies. All 12 subprocess recovery
tests pass, including vault commit, receipt, subsequent investigation rounds and
final synthesis boundaries. Ruff, mypy (560 files), Pyrefly (0 errors, 35
suppressed/77 warnings) and ty pass the error gates.

The first simple-interface live WAL probe at
artifacts/research-live-wal-simple-findings-20261030 exposed a deterministic
presentation error: the model copied the displayed [p...] brackets into the
passage_ref argument and repeated the rejected call. The diagnostic was
explicitly cancelled through its own durable task after the failure was
reproduced in follow-up; no successful finding/report is claimed. Passages now
display passage_ref=p..., and bracketed ids get a field-local exact correction
without silent normalization. A fresh web rerun is next; deterministic tests
alone do not establish semantic acceptance.

Still pending: structured per-question coverage, production semantic review
(not enabled), and reference-model comparison (no provider available). Current
work prioritizes the simple interface before expanding the knowledge model.
