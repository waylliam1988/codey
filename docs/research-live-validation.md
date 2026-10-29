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
