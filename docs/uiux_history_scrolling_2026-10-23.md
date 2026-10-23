# Conversation history and live reading

Global projectless chat creation was committed separately as ca048d9d.

This change removes the three persistent transcript links: Load earlier messages,
Show all for find, and Search this conversation. Approaching the top loads one
older page automatically. Ctrl/Cmd+F opens the existing conversation search, and
the command menu remains available. File inspection retains its own find action.
Loading is a transient small spinner; failure stops automatic retries and exposes
an accessible retry action. Requests time out after 15 seconds.

Upward user input stops following immediately. User scroll events take priority
over same-frame content growth. Background durable refreshes are deferred while
reading older messages; an in-flight tail refresh cannot discard loaded pages.
Returning to the latest messages reconciles the deferred history. Prepending
preserves mounted message nodes and the current reading anchor.

History cursors pin the visible ancestry head and revision. Appending entries to
the same branch cannot restart older pagination. A branch change invalidates the
snapshot explicitly. Duplicate/non-advancing cursors fail instead of looping.
The former unbounded load-all loop is removed.

Verification: frontend suite 201 passed; focused durable history/cursor/ancestry
and task-authority group 41 passed; production browser history test passed while
the backend appended a new entry during every older-page request. The browser
test also verifies the three links are absent, message nodes remain mounted,
exactly three requests retrieve the remaining pages, and Ctrl+F opens search.
TypeScript, Biome, Ruff, mypy, Pyrefly, ty and production build checks passed.
Pyrefly retains 29 existing suppressions and 79 existing warnings, with zero errors.

The long-conversation review page lives in isolated state under ignored artifacts.
The user's active desktop service is an older immutable release; it has not been
replaced or interrupted.
