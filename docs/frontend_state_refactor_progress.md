# Frontend state refactor progress

This document tracks the three required frontend refactor batches. The workspace
assembly batch is intentionally deferred.

## Batch 1: sidebar state

- [x] Move sidebar-only editing and preview expansion state into `sidebar.ts`.
- [x] Keep persisted `Project.expanded` owned by application state.
- [x] Keep `renameInputEl()` compatible with `ConversationUI`.
- [x] Verify search, project expansion, active-session preview, and rename behavior.

## Batch 2: run projection

- [ ] Make `RunProjection` the sole owner of active runs and turn mappings.
- [ ] Derive primary run/session instead of storing duplicate IDs.
- [ ] Keep lifecycle acceptance and snapshot owner semantics unchanged.
- [ ] Verify admission withdrawal, stale events, terminal states, and multiple runs.

## Batch 3: stream buffering

- [ ] Give the frame coalescer an explicit stream-buffer owner.
- [ ] Preserve event flush ordering and consume-time run checks.
- [ ] Verify late frames and final-message ordering.

## Verification log

Commands and results will be added after each batch. Full pytest is intentionally
not part of this work; targeted frontend checks and the repository's required
ruff/type checks are used instead.

### Batch 1 verification

- `npm run typecheck` — passed.
- `npm test -- --run src/sidebar.test.ts src/conversation_ui.test.ts src/ui_sync.test.ts src/frame_coalescer.test.ts` — 16 tests passed.
- `npx biome check --write src/sidebar.test.ts src/sidebar.ts src/app_runtime.ts` — passed.
- `git diff --check` — passed.
