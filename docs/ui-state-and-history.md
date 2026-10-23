# UI state and restored task history

## Ownership

`UiStateStore` remains the server owner of sidebar, chat configuration and draft
metadata. Its revision is minted by the server. Browser edits do not increment
that revision. Conversation content and task lifecycle facts remain in the
existing durable store; UI caching never persists message history.

The browser keeps a confirmed UI snapshot and its current edits. A single
`codey:ui-cache` localStorage envelope atomically pairs those two values. Existing
split cache keys are migrated once and removed only after the envelope is stored.
An installation with no confirmed baseline can recover a cached draft only for
a chat identity still present on the server.

## Synchronization

All writes use the confirmed server revision and one serialized save loop.
Acknowledgement applies only to the captured request snapshot. Edits made while
that request was in flight remain pending. Responses older than an already
accepted inventory cannot rewind the confirmed version.

On a conflict, the client reads current server state and uses one three-way
metadata merge. Unchanged fields follow the server; locally changed fields remain
pending. A remote deletion wins over a stale local edit, and an explicit local
deletion remains a deletion. New unrelated server chats/projects survive. Server-owned deletion identities are
committed in that same UI state file. They prevent a lost creation acknowledgement
from recreating an identity another window subsequently deleted. Clients cannot
clear those identities, even when submitting with the current revision.
Comparison ignores JSON object key order. A bounded reconciliation loop and
backoff prevent contention or an outage from causing an unbounded request loop.

Delete/clear responses return current UI state for that same synchronization
entry point. Clearing messages preserves chat metadata and drafts. Creating a
new chat does not issue a redundant purge request. Fork acceptance uses this
same merge instead of a separate fork-specific state merge.

Transient failures are silent and retry automatically, including on reconnect.
Repeated failures affecting a nonempty draft expose a specific draft warning.
If a send operation needs saved metadata and confirmation fails, that operation
reports that its message was not sent. Draft and session object identities remain
stable through ordinary acknowledgements.

## Model selection

All empty-chat creation uses one factory, including deletion of the last chat.
An enabled explicit choice is inherited with an independent effort map. A model
directory's first row and a connection default never choose a model implicitly.
An unconfigured chat displays `Choose model` and is not imported as an execution
configuration. Existing chats keep their selected identities if disabled or
unavailable; discovery does not replace them.

## Task history

History pages and focused history windows include checkpoint lifecycle versions
for the tasks referenced by their entries, read under the durable session's
mutation line. These are read projections of the existing checkpoints, not new
stored display events. Old pinned pages still report current task facts.

Both history loading and live state reconciliation feed `RunLifecycleProjection`
before updating Work or Stop. Older revisions cannot revive a settled task.
The renderer tracks whether its display came from a checkpoint, a workflow or
inferred message content; a final answer cannot override a Running checkpoint.
Tool history without lifecycle evidence renders neutral `Work`, without a running
spinner. Failed provider calls, including quota refusals, restore as Failed.
Restored active work uses the existing run-scoped cancellation path, which can
resolve a checkpoint even without a volatile registry owner.

Fork ancestry can include a task owned by another chat. Its history may be shown
in the child, but an active ancestor is neutral history there; Stop continues to
belong to the source chat. No task runtime or lifecycle fact is duplicated.
