# Steering and command approval

Implements the approved English interaction preview, preserving the navigation
sidebar, Settings, composer model dot and send icon.

- Pending inputs sit above the composer. Edit changes the queued text inline;
  Delete cancels it; Steer promotes the existing receipt. Sending during a run
  defaults to a follow-up, without a model-adjacent delivery selector.
- Queued text and mode revisions commit the pending task and unplaced submission
  together, fenced by checkpoint and conversation revision. Original admission
  identity, workspace, provider and model selection remain immutable. Once a
  task has claimed or placed the input, changes are rejected. The browser keeps
  unsaved edits available and reconciles uncertain responses by the same receipt.
- An unconsumed follow-up starts after the current task. A promoted steer is
  consumed once by a generation boundary; if the active turn has already ended,
  existing placement rules allow one ordinary follow-up instead.
- Approval is a compact Deny / Allow once split button. Its dropdown contains
  only exact-chat and, when supported, exact-project permissions. Native
  disclosure, focus handling and Escape support keyboard access.
- Scope errors remain visible in the pending card. Grant save failures stay
  beside the command result. The separate remember endpoint only writes grants
  for an already allowed invocation; it never schedules shell execution.
- History reconciliation inserts transient process and approval records before
  their own final answer. Late progress records also precede an already mounted
  answer; settled approval continuation cannot leave Waiting for approval active.

The interactive prototype remains in the thread visualization directory, with
an exported copy under ignored artifacts/uiux-preview/2026-10-23. It is separate
from product data. Verification results are recorded after the implementation
checks complete; desktop visual acceptance belongs to the user.

## Verification

- Frontend suite: 197 passed. Coverage includes inline edits, placement races retaining draft
  text, steering the same receipt, scope errors, and permission-only retry.
- Durable and approval regression group: 555 passed, 5 skipped. Memory and SQLite
  cases cover atomic edits, stale revision rejection, edit/placement races and
  prevention of writes to already placed submissions.
- Packaged frontend with isolated HTTP services and headless Chromium: 8 cases
  passed for once/chat/project approval, denied commands, lost responses,
  editing then steering, and automatic next-turn delivery across both native
  protocols. A final dropdown lifecycle check also passed.
- TypeScript, Biome, Ruff, mypy, Pyrefly and ty checks passed. Pyrefly retained
  29 existing suppressions and 79 existing warnings; it reported zero errors.
- Production Vite build and published asset consistency checks passed.

These groups overlap and are not a full-repository test total. The product review
page uses separate local state and a pending read-only clock command; it does not
switch the user's running desktop service or execute the command automatically.
