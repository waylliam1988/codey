# Frontend state refactor progress

This document tracks the three required frontend refactor batches. The workspace
assembly batch is intentionally deferred.

## Batch 1: sidebar state

- [x] Move sidebar-only editing and preview expansion state into `sidebar.ts`.
- [x] Keep persisted `Project.expanded` owned by application state.
- [x] Keep `renameInputEl()` compatible with `ConversationUI`.
- [x] Verify search, project expansion, active-session preview, and rename behavior.

## Batch 2: run projection

- [x] Make `RunProjection` the sole owner of active runs and turn mappings.
- [x] Derive primary run/session instead of storing duplicate IDs.
- [x] Keep lifecycle acceptance and snapshot owner semantics unchanged.
- [x] Verify admission withdrawal, stale events, terminal states, and multiple runs.

## Batch 3: stream buffering

- [x] Give the frame coalescer an explicit stream-buffer owner.
- [x] Preserve event flush ordering and consume-time run checks.
- [x] Verify late frames and final-message ordering.

## Verification log

Commands and results will be added after each batch. Full pytest is intentionally
not part of this work; targeted frontend checks and the repository's required
ruff/type checks are used instead.

### Batch 1 verification

- `npm run typecheck` — passed.
- `npm test -- --run src/sidebar.test.ts src/conversation_ui.test.ts src/ui_sync.test.ts src/frame_coalescer.test.ts` — 16 tests passed.
- `npx biome check --write src/sidebar.test.ts src/sidebar.ts src/app_runtime.ts` — passed.

### Batch 2 verification

- `npm run typecheck` — passed.
- `npm test -- --run src/run_projection.test.ts src/run_lifecycle.test.ts src/reply_stream.test.ts src/conversation_ui.test.ts` — 17 tests passed.
- `npx biome check --write src/app_runtime.ts src/run_projection.ts src/run_projection.test.ts` — passed.

### Batch 3 verification

- `npm run typecheck` — passed.
- `npm test -- --run src/run_stream_buffer.test.ts src/frame_coalescer.test.ts src/run_projection.test.ts src/run_lifecycle.test.ts src/reply_stream.test.ts` — 19 tests passed.
- `npx biome check --write src/app_runtime.ts src/run_stream_buffer.ts src/run_stream_buffer.test.ts` — passed.

### Final verification

- `npm run check` — passed; 58 test files and 346 tests passed.
- `npm run build` — passed.
- `ruff check codey tests tools` — passed.
- `git diff --check` — passed.
- No `any` was added in the new sidebar, run projection, or stream buffer files.
- `git diff --check` — passed.
