# Persistent research knowledge implementation

Authorized scope: one shared execution kernel; SQLite authority; reusable,
versioned evidence and conditional claims; cross-session recall; graph provenance;
generic research methods. Markdown is interchange, never a competing authority.

## Plan and acceptance

1. SQLite authority behind KnowledgeStore, one-time legacy import, immutable
   revisions, explicit import/export, database-aware compensation and atomic batches.
2. Long-term knowledge model and idempotent commits, evidence relations and pinned
   dependencies; preserve unknown conditions and provenance, enforce scopes.
3. Shared tool/context integration, bounded alias/relationship recall, research
   guidance and domain checks; graph and reports reference persisted versions.
4. Exercise restart/recall/revision and crash windows; targeted regressions,
   Ruff, mypy, Pyrefly, ty, frontend checks if touched; local model experiment.

## Constraints

- No second worker, scheduler, effect ledger, retry loop or provider client.
- Semantic similarity never merges identities or increases evidence strength.
- Existing report objects remain bounded projections, not long-term authority.
- Hypotheses, observations, author interpretations and derived conclusions retain
  their identities. Old answers are not independent supporting evidence.
- Privacy deletion purges content and derived projections; withdrawal retains history.
- Do not run full pytest; expand targeted checks when findings justify it.

## Status

Parent synthesis is now connected after evidence investigation through the same
driver/continuation and completion gate. Crashes at final response/settlement
resume without another model request or another evidence write. A synthesis
failure never restores the old successful draft. Source locator ranking now
avoids repeated TOC hits and distinguishes page offsets from receipt offsets.
The real WAL run exercised final synthesis successfully but still failed content
review. A fresh real medical comparison with local thinking enabled is running.

Latest continuation: acquisition-only follow-ups now avoid repeating parent
reports and forbidden final-answer references, and must actually attempt
acquisition before finishing. 105 focused tests and all four static checks
passed. The preceding live WAL run ended normally but omitted requested
version/workload comparisons; it remains unaccepted. Fresh targeted-investigation
browser retest is running; see the live validation log for results.

Current delivery: implementation and deterministic production acceptance complete.
Local Gemma4 persistence/recall/revision has now passed in three independent
processes, most recently 8/4/4 turns with request-scoped contracts. Its exported
revision graph has seven nodes/seven edges and retains pinned version identities.
Real broad browser research is still NOT accepted: source acquisition works,
but coverage and interpretation failures remain. Historical failures below must
not be read as current lifecycle status; see the latest continuation at the end
and docs/research-live-validation.md for the live research audit.

- Baseline inspected: d306aa8c, working tree clean.
- Existing research continuation already persists references to shared operation
  results. Material acquisition and synthesis/staged writes still need scrutiny.
- Existing KnowledgeStore reads Markdown; index rebuild scans files. Existing
  common context only recalls the current session's synthesis.
- Step 1 implemented: `.codey/knowledge.db` owns document heads and immutable
  revisions; old Markdown and index.db remain unchanged during one-time migration.
  Explicit import requires an expected revision; exports cannot change the DB.
  Rebuild uses canonical documents. Staged batches use SQLite rollback rather than
  filesystem compensation. Undo creates compensation revisions with conflict checks.
- Validation: 100 targeted tests passed (knowledge, SQLite authority, changes,
  continuation contracts, pipeline). Ruff and targeted strict mypy passed.
- Step 2 implemented: independent long-term object/version model, pinned typed
  references, explicit origins and unknown-condition states, idempotent commits,
  optimistic revision checks, scope validation and cyclic-premise rejection.
  Verified knowledge_write excerpts automatically archive full source versions,
  fragments and claims in the note transaction. Reports remain documents.
  Staged proposal receipts now preserve their original structured batches.
- Step 3 in progress: shared-kernel fresh prompts and knowledge_search/read use
  bounded alias/text/relationship recall, with reasons, conditions and transitive
  dependency review flags. knowledge_commit uses the existing safe effect replay
  path because its transaction ID and digest make it idempotent. No new runtime.
- Validation: 256 existing focused tests passed before adding shared recall; 56
  focused storage/recall/protocol tests passed after integration. Full production
  Ruff, mypy, Pyrefly and ty passed (Pyrefly retains existing warnings).
- Remaining: provenance graph UI, general research guidance and checks,
  acquisition/synthesis effect boundaries, driver crash-window tests, local model
  experiment, targeted expansion and final checks. Privacy purge covers knowledge
  objects/dependents; existing durable outputs/receipts retain their own lifecycle.

Update this file in each implementation commit with results and remaining work.

## Final integration and acceptance (2026-10-29)

- Added a stable owner identity per knowledge SQLite database. Authorized personal
  observations survive conversation changes; reports inherit restricted premise
  scope. This is one owner's isolated knowledge space, not a multi-user ACL.
  Combining incompatible restricted scopes fails explicitly.
- Both native and text tools now advertise the same detailed nested mutation,
  evidence, condition and pinned-reference schemas. Ledger field aliases remain
  accepted. Repository validation remains the semantic authority.
- Recovered tool outcomes without the original persisted authorization are
  rejected. This also fixes the previously observed entry-policy regression.
- Repeated staged updates now include the target in every update receipt, not
  only its first appearance. Acceptance restores three successive writes before
  and after the SQLite commit, preserving all fragments with one claim version.
- Production-entry acceptance passes acquisition, fresh AppContext recall with
  different wording, candidate analogy creation, CAS premise revision, stale
  dependency notification and graph inspection of old/new pinned versions.
  This uses deterministic model replies; it does not measure model reasoning.
- Focused expansion: 279 tests and 433 subtests passed; one architecture test
  fails on baseline providers/api_provider.py exceeding 1000 lines. Verified the
  file is byte-for-byte unchanged from d306aa8c, at 1028 lines. No baseline
  exemption or unrelated provider refactor was added. An additional 87 scoped
  entry/lifecycle tests, 34 schema/acceptance tests and 15 staged lifecycle tests
  passed (these groups overlap; do not sum them as unique tests).
- Production Ruff, mypy (551 files), Pyrefly and ty pass; Pyrefly still reports
  its existing warning/suppression counts. Published frontend assets check passes.
- Remaining verification: final research/protocol expansion and local model probe
  with the production local response decoder. Earlier native probe timed out;
  recall successfully read existing knowledge but one attempt ended with an
  unknown generation outcome, which was correctly not resent. Full three-stage
  live acceptance has not yet passed. Keep this distinction in the final report.

## Local transport correction

- Final research/protocol/entry expansion: 291 tests and 7 subtests passed.
- The live probe initially constructed ApiProvider without the local response
  decoder used by the application. Corrected it to use normalize_local_reply.
  Earlier results remain diagnostic attempts, not a passing application-model
  benchmark. The probe now also checks the actual persisted changes after a
  successful final answer, rather than trusting exit status alone.
- A malformed local tool frame must not become a successful prose completion in
  native mode. The local decoder now marks it as a protocol error, and the chat
  codec preserves that verdict without recording a fictitious assistant result.
  Canonical turns pass through unchanged; valid native calls retain precedence.
- 48 focused local decoder, real transport-boundary and exchange tests passed.
  Production Ruff, mypy, Pyrefly and ty passed again after this correction.

## Final verification (2026-10-30)

- Final combined local-protocol, transport, production knowledge acceptance and
  lifecycle regression: 64 tests passed. No full pytest run was performed.
- The production-codec live attempt saved verified fact evidence but its initial
  research stage exceeded the probe's 420-second timeout. A separate subsequent
  recall request received an incomplete/invalid generation response and stopped
  with GenerationUnknownError without automatic resend. No live three-stage
  pass is claimed. Artifacts are retained in
  `artifacts/persistent-research-production-codec-20261029/` (ignored runtime data).
- The automated full lifecycle uses scripted provider responses through the real
  task entry. The nine subprocess failure/restart scenarios verify the existing
  driver/effect path separately. Neither test claims to validate arbitrary model
  reasoning, source search quality or individual treatment applicability.
- Implementation and focused verification are delivered. Remaining limitations:
  local-model end-to-end stability, the pre-existing architecture line-count
  failure, and the explicitly documented scope/deletion and semantic-evaluation
  boundaries. No second runtime, vector service or automatic identity merger was
  introduced. Usage and storage migration details: `docs/persistent-research.md`.

## Versioned reports, graphs and lifecycle

- Reports now save independent answer objects and pin adopted knowledge versions;
  done.knowledge_refs is mechanically checked for existence, scope and type.
  Settled report projection identities/digests prevent silent rewrites on replay.
- Session graphs use the same pinned versions and evidence relations. Node/edge
  details expose conditions, origins, references and review flags. The frontend
  supports edge inspection and candidate styling; built assets are published.
- Explicit knowledge commits participate in undo. Privacy purge removes dependent
  knowledge, document history, indexes and managed note exports, and tombstones
  affected commits against resurrection. Application execution receipts, external
  backups and managed acquisition outputs retain their separate lifecycle.
- Research-specific evidence extraction lives in research/knowledge_archive.py;
  the shared knowledge layer has no upward research imports.
- Fixed mixed tasks without project directories being forced into strict research.
  Ordinary coding/chat/planning can read scoped knowledge; explicit denial wins.
  An explicit headless knowledge root now applies to mixed and ordinary tasks.
- Initial kernel failures propagate their original diagnosis; strict research can
  read stored long results. Conditions have consistent object schemas. Tool writes
  expose structured ids, distinct from document ids. EOF errors request a complete
  corrected JSON request without repairing model data automatically.
- Repeated staged updates retain earlier evidence and their original document
  preconditions; receipts preserve cumulative proposals for restart.
- Validation so far: 79 knowledge/pipeline/lifecycle tests; 36 protocol/replay/
  recovery tests; 65 knowledge/system-action/completion tests passed. Expanded
  entry suite: 151 passed, with an unrelated existing policy-recovery test failure.
  Frontend: 280 tests and build/publish checks passed. Production mypy passed
  (550 files); Ruff passed. Final expanded checks still pending.
- Local Gemma4 probe uses production headless/kernel in separate processes and a
  frozen public SQLite documentation corpus. Evidence acquisition/storage works;
  text protocol attempts exposed malformed JSON and tool-token output, and one
  unknown generation outcome was correctly not resent. Complete live three-stage
  acceptance has NOT passed yet. Native protocol experiment and final verification
  remain in progress; failures are retained under ignored artifacts/ directories.

## Shared investigation integration

- Production follow-up now proposes searches, reads and evidence writes through
  the existing shared task kernel. PlanExecutor is retained only for explicit
  legacy/test callers; research_flow no longer calls its direct acquisition path.
- Immutable plan/baseline references use the existing continuation artifact;
  changed admitted inputs fail closed. There is no research execution cursor.
- Per-invocation query/open bounds use normal receipt projections and in-flight
  reservations, including restart reconstruction. The operation still owns all
  request/turn budgets and unknown-send handling.
- Ordinary staged write receipts now include original proposals and structured
  batches. Identical staged source/entity/evidence objects can share one initial
  version without weakening optimistic concurrency for ordinary commits.
- Generic, composable research method guidance is integrated into strict research.
- Validation: 9 subprocess restart/failure tests passed (plan admission, provider
  reply, tool delivery, DB transaction and second round; lost acknowledgement;
  not-sent versus unknown outcome). 66 focused repository/pipeline/policy tests
  passed. Production Ruff passed. Remaining: report/version graph, lifecycle
  cleanup, local model validation and final static checks.


## Local real-web validation continuation (2026-10-30)

- Controlled persistence/recall/revision has passed in independent processes,
  with mixed thinking/JSON configurations; recall read its premise after the
  candidate commit. This is lifecycle acceptance, not full research quality.
- Production local configuration now offers optional JSON-constrained task
  calls with thinking off, frozen/recovered by the same kernel/provider path.
  Plain remains default; the user configuration is not silently changed.
- Real WAL and adaptive-therapy browser tasks exposed citation-group handling,
  wrong note-vs-claim references, spliced quotes, footer-only responses and
  misleading compaction summaries. Fixes are committed; failed runs retained.
- Research results and repair continuations now show bounded receipt-derived
  opened-source identities, saved evidence and recent actual failures. Summary
  prose cannot amend these facts. Evidence-only done repair preserves its own
  task format. Exact claim-version candidates lead legacy document reads.
- The receipt-guided medicine run searched/opened actual pages and saved two
  evidence notes, but then repeated invalid document references; it was stopped
  through the durable cancellation barrier and is not accepted. Repair feedback
  now addresses the invalid reference first; all other checks still rerun.
- Same-configuration lifecycle accepted: artifacts/research-three-stage-json-prerequisites-20261030,
  three independent processes, production JSON mode/thinking off (7/4/4 turns),
  read-before-commit and exact candidate v1-to-v2 revision. Graph: 7 nodes/7 edges.
  This proves lifecycle, not the queue analogy's semantic validity.
- Fresh WAL and medicine live-web report quality remain unaccepted. Browser
  acquisition works; premature synthesis, missing coverage and repeated rejected
  drafts remain under diagnosis. Thinking/low additionally produced a template
  marker loop. Rejected full-argument grammar experiments are retained; shipped
  JSON grammar constrains tool names/order only and leaves arguments unordered.
- Current diagnostic: artifacts/research-live-wal-localized-20261030. Concise
  Chinese continuation guidance is being evaluated; no quality pass claimed yet.
- Detailed commands, results and failure distinctions are maintained in
  docs/research-live-validation.md. No full pytest run was used.

## Current browser diagnosis and fixes (2026-10-30)

- Corrected latest-result ordering and froze JSON tool catalogs per request,
  preserving their original prepared-invocation/recovery identity.
- Cancelled follow-up investigations now propagate cancellation, rather than
  returning an earlier draft as done. A production durable-driver test covers it.
- Missing-finding repair prioritizes acquisition over unrelated saved wording.
- Full claims survive knowledge storage/recovery; UI clipping cannot erase a
  trailing applicability condition. Explicit nonsignificant comparisons cannot
  silently become affirmative significant support in the evidence write path.
  This narrow guard does not establish general entailment or statistical validity.
- Trusted text-tool result spans can use existing receipt-backed deterministic
  compaction, with originals preserved and newest source protected across repairs.
  Soft admission applies these views without another model generation.
- Latest runtime observations also use frozen request-only arguments. Every
  real JSON request now contains one current research snapshot rather than
  appending all prior snapshots. Plain/native behavior is unchanged.
- Failed WAL/medical runs remain isolated in artifacts, with durable cancelled
  outcomes. They are not accepted research knowledge. New live task under
  inspection: research-live-adaptive-current-state-20261030.
- Latest related checks: 51 prepared-request/JSON/evidence tests, 53 compaction/
  delivery tests (three optional skips), 20 research observation/completion/
  subprocess-restart tests passed. Ruff, mypy (555 files), Pyrefly and ty passed.
- Remaining: accepted broad live reports (human/animal/registration distinctions
  and full WAL coverage); final lifecycle rerun on all changes; final artifact
  inspection and documentation. No claim that Gemma4 now runs long research reliably.

- Follow-up prompts now carry bounded ledger findings instead of old reports;
  evidence-only phases require an actual acquisition receipt. Production adds
  gated parent synthesis after investigation through the same durable driver.
- Request-only observations/contracts now cover ordinary text and native API
  protocols, with grammar exclusive to JSON. Real thinking/plain requests retain
  one current snapshot. Focused protocol/recovery checks passed; the live medicine
  task still failed semantic review and looped on missing source-table URLs.
- Next diagnosis: prioritize citation-table defects before claim-binding repair,
  retain explicit trial uncertainty, and verify actual requested human outcomes.
- Implemented focused citation-table repair priority, explicit may/might-effect
  preservation on evidence saves and trailing comparison/check term retention.
  50 evidence/report tests and 19 proof tests passed; Ruff/mypy/Pyrefly/ty passed.
  New real-browser medicine run is in progress, not yet quality accepted.
- That run ended protocol-failed. Final synthesis re-recalled its own saved
  initial report; automatic background now excludes answer objects before seed
  limits/graph traversal. Explicit historical search/read remains available.
  Child-phase write/read/search receipt guidance now omits parent-only answer
  reference instructions. Removed generic no-counter templates and clarified
  independent comparator searches. 80 focused tests and all static checks passed.
- JSON broad-task diagnostic still failed on empty argument keys and spliced
  excerpts. Added focused unknown-property repair and explicit saved-extraction
  audit guidance; 60 pipeline/restart/protocol tests, 19 repair/quote tests and
  ten synthesis/observation tests passed (overlap). All static checks passed.
- Native WAL probe exposed two more concrete defects: stale first-hit examples
  and silent creation on an unknown explicit note update id. Both are fixed;
  current query targets survive receipt replay, and invalid updates write
  nothing. 54 focused tests and 52 source/feedback tests passed (overlap), all
  static checks passed. Fresh three-process final-code lifecycle is running.
- That lifecycle was cancelled after repeated citation/binding failures; it did
  not pass. Feedback now prioritizes a single report-quality repair before
  binding hints when sections exist. The manual control fixture explicitly asks
  for full saved sentences and individually cited paragraphs; this is a fixture
  clarification, not a broad research quality claim. 26 focused format tests
  and 44 completion/lifecycle/restart tests passed (overlap), static checks passed.
- The controlled three-process suite now completes (fresh focused-final artifact):
  save, restart/recall, candidate analogy, pinned revision update all validated.
  Compound saved claims and an incomplete final explanation remain quality
  limitations; this is not a broad research pass. Counter-search gaps can now
  be reported honestly without counting as a completed check, and Chinese repair
  reminders distinguish formatting from evidence acquisition. Real web WAL
  thinking/low probe in progress. Default model settings are unchanged.
