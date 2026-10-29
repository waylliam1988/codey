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
