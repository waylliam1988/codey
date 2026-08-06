"""Ghost continuity primitives for Codey.

The 0.3.0 Ghost layer only extracts explicit learning signals. It does not
write accepted long-term memory or alter agent behavior.
"""

from codey.ghost.schema import GhostSignal, GhostSignalParseResult
from codey.ghost.store import GhostSignalStore

__all__ = [
    "GhostSignal",
    "GhostSignalParseResult",
    "GhostSignalStore",
]
