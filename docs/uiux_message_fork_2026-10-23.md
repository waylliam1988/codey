# Message actions and chat forks — 2026-10-23

Implements the approved interactive reference in `artifacts/uiux-preview/2026-10-23/`.
Fixed UI copy is English; typography, quiet field focus and monochrome controls
follow `DESIGN.md`. The existing navigation, Settings and composer are retained.

## Behavior

- User actions: Copy, Edit, Fork; assistant actions: Copy, Fork. Actions remain
  below the message and appear on hover, keyboard focus or touch.
- Clicking Fork opens a compact destination dialog directly. Git projects offer
  the current workspace/worktree and a new worktree. Ordinary directories fork
  directly into the current directory.
- A message fork retains its complete turn, excludes later turns and creates an
  independent chat without starting a provider task. Existing History controls
  retain their explicit entry-based behavior.
- The new chat keeps the source title and model selection. `Continued from chat`
  returns to the source chat. The inherited history and source are persisted by
  the existing durable conversation ancestry.
- Worktree setup registers its accepted child immediately, displays a pending
  state and blocks sending until ready. Failure and restart interruption offer
  Retry using the same child identity. A completed late response is registered
  without taking over a different chat the user has opened.

## Git ownership and state

Worktrees live under the server's `state_home/chat-forks/`. Each accepted setup
has an atomic local receipt. Setup uses a separate temporary Git index and a
detached snapshot commit to capture tracked edits and non-ignored untracked
files. It does not stage, stash, reset, commit on, or move the source branch.
The snapshot is the current working state, not a historical filesystem rollback.
Nested project directories retain their relative path inside the new worktree.

The created worktree gets a real project entry so execution and document scopes
use its own directory. This adds user-created resources to the existing sidebar;
it does not change the sidebar layout or Settings. Retry never overwrites an
unrelated destination or deletes files. Worktrees persist for later use and are
not automatically removed when a chat or project is hidden.

## Validation boundary

Verification uses frontend unit tests, Python route tests, and actual temporary
Git repositories, including staged/unstaged/untracked/ignored files, source
index preservation, whole-turn ancestry, repeated identities, pending input
guards, setup failure/retry and interrupted receipts. TypeScript, Ruff, mypy,
Pyrefly, ty, production build and asset publication are checked separately.

At the user's request, this implementation is not inspected by controlling the
desktop or interacting with a browser. An updated product page is opened for
the user's visual review. No screenshot or manual browser acceptance is claimed.

Final checks: all 188 frontend unit tests passed; 39 scoped Python tests passed
(fork routes, real Git worktrees, input guards, durable ancestry/revision fencing,
and frontend publication contracts). TypeScript, Biome, Ruff, mypy, Pyrefly and ty
passed; Pyrefly retains the repository's existing suppressions and warnings.
Production assets were built and published with `tools.publish_frontend`, and
the publisher's consistency check passed. Reports are saved beside the approved
preview under `artifacts/uiux-preview/2026-10-23/`.

The review page uses the real packaged frontend and routes with an isolated
state directory and two English example turns. It does not replace the user's
existing chat database or restart an existing Codey service.
