# Conversation deletion and history pages

Deleting a conversation targets that conversation. A retained fork currently
depends on its parent's entries and artifacts, so deleting a parent with forked
children returns HTTP 409. Both conversations and their sidebar entries remain
available; the fork can continue normally. Deleting a child does not delete its
parent. Automatic fork detachment and cascade deletion are separate features.

Durable deletion runs before projection cleanup. Storage failures stop cleanup;
partial projection cleanup returns HTTP 503 with `ok: false` and the unpurged
stores. The sidebar entry remains available for retry, including after restart.
Successful retries also work when the durable conversation is already absent.
Only complete cleanup permits removing the sidebar entry. The frontend treats
conflicts and incomplete cleanup as failures and displays the server error.

The SQLite delete transaction removes private workflow checkpoints and effect
bodies from commit history along with the conversation's business records.
Public commit sequence numbers, bounded identity projections and the deletion
event remain intact for event replay. Other conversations' timelines remain
unchanged. Managed output files and execution output snapshots are cleaned by
session; cleanup failures can be retried. This is application data removal, not
a promise to overwrite database free pages, backups or user-owned workspace files.

History pagination walks the active ancestry using metadata and indexed local
parent links. It loads full entry bodies and response artifacts only for the
selected page. SQLite reads the head, ancestry, entries, task states and artifact
metadata in one read transaction. Pinned cursors, inherited fork caps, edits and
head markers keep their existing behavior. Computing ordinals and inactive run
IDs still visits metadata; query cost is not independent of history length.

Shell approval continuation also preserves the original run identity. HTTP
admission may lose its durable claim to the scheduler; it must leave a claimed
worker's reservation intact, including between the terminal database commit and
`task_done` publication. Replays may release only an unstarted reservation for a
parked or terminal task. That status check and conditional release share the
durable control line so a concurrent approval cannot requeue between them.
