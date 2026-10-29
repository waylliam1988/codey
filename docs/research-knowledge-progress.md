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
