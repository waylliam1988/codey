# Message actions and chat forks — 2026-10-23

Implements the approved interactive reference in `artifacts/uiux-preview/2026-10-23/`.
Fixed UI copy is English; typography, quiet field focus and monochrome controls
follow `DESIGN.md`. The existing navigation, Settings and composer are retained.

## Behavior

- User actions: Copy, Edit, Fork; assistant actions: Copy, Fork. Actions remain
  below the message and appear on hover, keyboard focus or touch.
- Clicking Fork opens a compact destination dialog directly and waits for an
  explicit choice, including in ordinary directories and projectless chats.
  The local destination remains available while checking Git. A new worktree
  is enabled only after a successful capability check; unsupported destinations
  show their reason, and failed checks offer Retry without blocking local forks.
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

## Explicit destination selection revision — 2026-11-06

Capability checks no longer submit a local fork. Both destinations remain in
the dialog, including for projectless chats, ordinary directories and empty Git
repositories. Git failures (missing executable, missing directory, timeout or
other command errors) are distinct from unsupported workspaces and provide an
explicit workspace-check retry. Creation retries retain the accepted request
identity and never enable an unavailable worktree destination. Late checks and
dismissed dialogs cannot create chats or re-enable submitting controls.

Validation for this revision: 35 scoped frontend tests passed, including 21 fork
dialog tests; 20 scoped Python capability/worktree tests and 53 ancestry,
branch-revision, workflow-fork and frontend-publication regression tests passed.
Four headless Chromium tests used the shipped frontend and real fork routes for
projectless, ordinary-directory, Git and timeout/retry scenarios, including
checking, dismissal, explicit selection, navigation and inherited history.
TypeScript, scoped Ruff and mypy checks passed, and production resources were
rebuilt and checked with `tools.publish_frontend`. Full pytest was not run.
