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
- Remaining: structured claims/evidence commits, shared recall/tools and graph,
  acquisition/synthesis effect boundaries, expanded static/restart/model checks.

Update this file in each implementation commit with results and remaining work.
