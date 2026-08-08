"""Ghost continuity primitives for Codey.

The 0.3.2 Ghost layer can project explicit learning signals into an inbox and
reinforce accepted candidates into bounded local Hebbian state.  It still does
not alter agent behavior.
"""

from codey.ghost.hebbian import GhostEdge, GhostHebbianStore, GhostNode
from codey.ghost.inbox import GhostInboxStore, GhostMemoryCandidate
from codey.ghost.schema import GhostSignal, GhostSignalParseResult
from codey.ghost.store import GhostSignalStore

__all__ = [
    "GhostEdge",
    "GhostHebbianStore",
    "GhostInboxStore",
    "GhostMemoryCandidate",
    "GhostNode",
    "GhostSignal",
    "GhostSignalParseResult",
    "GhostSignalStore",
]
