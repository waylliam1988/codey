class DurableStoreError(RuntimeError):
    """Base class for durable store failures."""


class CommitConflict(DurableStoreError):
    """The caller's expected conversation revision or parent is stale."""


class SchedulerOwnershipError(DurableStoreError):
    """Another live scheduler currently owns this durable database."""


class IdempotencyConflict(DurableStoreError):
    """A request ID was reused for a different submission."""


class SchemaVersionError(DurableStoreError):
    """The database schema is newer than this application supports."""


class SessionSealedError(DurableStoreError):
    """The open session can no longer accept writes after a storage failure."""


class SessionFailedError(SessionSealedError):
    """The session failed after a durable commit or internal adoption error."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__("durable session failed; close and reopen it")
        self.cause = cause
