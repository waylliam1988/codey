class DurableStoreError(RuntimeError):
    """Base class for durable store failures."""


class CommitConflict(DurableStoreError):
    """The caller's expected conversation revision or parent is stale."""


class IdempotencyConflict(DurableStoreError):
    """A request ID was reused for a different submission."""


class SchemaVersionError(DurableStoreError):
    """The database schema is newer than this application supports."""


class SessionSealedError(DurableStoreError):
    """The open session can no longer accept writes after a storage failure."""
