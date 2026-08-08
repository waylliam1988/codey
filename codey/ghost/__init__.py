"""Ghost continuity primitives for Codey.

The 0.3.1 Ghost layer extracts explicit learning signals and can project them
into a local memory inbox.  It still does not alter agent behavior.
"""

from codey.ghost.inbox import GhostInboxStore, GhostMemoryCandidate
from codey.ghost.schema import GhostSignal, GhostSignalParseResult
from codey.ghost.store import GhostSignalStore

__all__ = [
    "GhostInboxStore",
    "GhostMemoryCandidate",
    "GhostSignal",
    "GhostSignalParseResult",
    "GhostSignalStore",
]
