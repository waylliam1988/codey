# Shell approval lifecycle

`ShellApprovalWorkflow` owns HTTP decisions, durable work, execution and continuation.
HTTP handlers acknowledge a committed decision with `202`; they do not wait for the
shell or the model. The command artifact is verified before accepting a decision.

## State and identity

Non-allowlisted singleton `run` calls enter the same approval workflow as
`shell`, after checking the task grant, effective project profile and path
policy. Hard denials and unavailable approval channels do not create waiting
cards. Unsupported Windows heredoc syntax is rejected with a compatibility
explanation before approval. The command is never rewritten after consent.
The immutable artifact preserves the original tool name, arguments and call
ID through execution, native delivery and recovery. An approved arbitrary
command does not become trusted verification merely because it used `run`.

`run.output_tail_lines` bounds the displayed/model output while retaining the
actual process exit code and existing output capture. The command card can
also toggle the last 40 retained lines without changing the copied output.

Approval choices are once, exact command in this conversation, and (for
narrowly supported simple commands) exact command in this project. Grants
bind canonical project/cwd, shell and an environment fingerprint. Compound
commands and inline scripts cannot create project rules. Rules live in the
user state directory, never in repository-controlled configuration. Settings
and command cards expose the saved rules and revocation. Revocation and the
final remembered-grant check share the spawn gate. Corrupt/unavailable rule
storage falls back to explicit approval. Automatic acceptance happens on an
independent approval worker after the task is parked, so it cannot reuse a
released task-attempt token. Every execution still requires a durable claim.

Mixed tool batches retain ordinary per-tool execution and refusal; commands
requiring approval must be submitted alone so earlier effects cannot be
silently skipped or replayed.

The fact journal binds every approval to its session, source run and verified command
artifact. Native call IDs, protocol windows, workspace attachment and API model
selection are reconstructed from that source, rather than from the current UI.

The query projection separates `decision`, `execution` and `continuation`, with a
revision derived from the durable history. An execution claim without a trusted
receipt is uncertain and cannot automatically execute again. A missing or invalid
settled receipt also blocks redelivery. This does not promise exactly-once external
side effects across a process crash.
Receipt unavailability takes precedence over an earlier continuation lifecycle
state for both Allow and Deny. A failed continuation cannot expose a retry action
when the saved result is no longer verifiable.

A continuation run ID is saved before submission. Retries retain that identity. At
startup, a queued continuation is resubmitted only if its runtime operation has not
initialized; once an operation exists, subsequent user continuation uses `ResumeRun`, not a new
shell execution. A failed continuation can be retried independently of the command.

## API and UI

- `POST /api/shell_approval`: atomically accept `{id, approved}`. The same decision
  returns existing state; the opposite decision returns `409 decision_conflict`.
- `GET /api/shell_approval?id=...`: authoritative state for response-loss recovery.
- `GET /api/shell_approval?session_id=...`: bounded state list for a session.
- `POST /api/shell_approval/retry`: retry delivery/continuation, never shell execution.
- `/api/state`: current approval projections and background approval busy ownership.

The UI stores state by approval ID, discards older revisions, and keeps an uncertain
snapshot from being overwritten by an equal-revision buffered live event. A lost
POST response triggers a status query. An unconfirmed query leaves decisions locked
with a separate status-check action. Async callbacks hold no references to approval
buttons or error nodes. Failed continuation has its own retry action.

Continue uses server state. Pending approval directs the user to its card; execution
in progress remains busy with Stop available; a saved result uses approval resolution;
an already-started continuation uses its own run ID. Starting another task cannot
silently open a fresh native protocol window over an unresolved approval.

## Ordering and shutdown

The spawn gate precedes the context lock for Stop and execution admission. The worker
coordination condition is never held while publishing an event, acquiring application
locks, reading artifacts, or calling submission code. Close waits for worker completion
before releasing durable resources.

Execution results are written as verified artifacts and settled facts before publishing
`shell_result`. Explicit Stop durably cancels queued work and continuation; cancellation
is checked again under the submission lock before reserving a run, even if another task
has cleared the transient stop flag. Graceful shutdown preserves unanswered requests,
accepted-but-unclaimed work, and saved results awaiting continuation for restart.
Pending-card expiry also persists its abandoned verdict before removing the memory
projection. A failed journal write propagates as an error and leaves the card
queryable; retrying Stop after storage recovers commits the verdict and removes it.
The failure path still invalidates the approval generation, so a previously
claimed execution ticket cannot become runnable when new work clears the
transient stop flag. Successful expiry retains the original single generation bump.

## Regression coverage

`tests/test_shell_approval_lifecycle.py` covers response timing, idempotency, disk
failure, execution uncertainty, Stop races, shutdown/restart windows, continuation
identity, UI persistence, busy ownership and lock ordering.

`tests/test_shell_approval_browser.py` exercises the packaged UI with actual HTTP and
subprocess execution, dropped approval responses, Allow/Deny and continuation retry.
`tests/test_shell_approval_http_restart.py` also checks real HTTP Stop failure and
retry with an injected journal I/O error; this does not simulate physical disk failure.
`tests/test_release_shell_continuation_http.py` uses local deterministic model peers
for both native API protocols and verifies the original call/window identity.

## API transport cancellation

Stop interrupts all owned sockets before closing buffered response wrappers.
The cancellation path recognizes sockets retained after HTTP connection handoff
and detaches/closes their native handles: ordinary `socket.close()` can defer
native closure while a `makefile()` reader still holds a reference. This matters
for Windows readers blocked on a silent HTTP error body. Pool leases remain the
authority for whether a connection is still owned by the cancelled request.

Expected I/O and closed-stream errors become cancellation errors only after the
attempt is cancelled; unrelated read errors remain unchanged. Regression checks
use real socket pairs, real local HTTP servers and repeated Stop during both
header waits and silent rejection bodies. They retain the existing one-second
responsiveness assertion rather than increasing its timeout.
