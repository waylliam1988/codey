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
- `research-native-thinking-20261030`: native stream + reasoning, in progress.
  Initial report passed the mechanical gate, but its second excerpt did not
  directly establish a single writer. The ordinary shared investigation then
  explicitly identified that evidence gap. Inspect its final persisted results
  before treating this run as a research-quality pass.

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

## Remaining work

1. Finish successful real three-process research / recall / revision validation,
   inspecting actual quotes and persisted conditions, not just exit status.
2. Rerun the real SQLite browser task after fixes and assess answer correctness.
3. Run a real non-software browser research task to check generalization.
4. Complete targeted recovery/protocol/acquisition checks and production Ruff,
   mypy, Pyrefly and ty; update intentional prompt fixtures after review.
5. Record exact successful and failed artifacts and commit remaining changes.

Frontend unchanged in this iteration. Full pytest is intentionally not requested.
