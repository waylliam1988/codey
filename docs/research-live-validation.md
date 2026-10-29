# Local Research validation, 2026-10-30

Work in progress. Do not infer a live acceptance pass from deterministic tests.

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
