"""Ghost continuity primitives for Codey.

The Ghost layer can project explicit learning signals into an inbox, reinforce
accepted candidates into bounded local Hebbian state, and render confirmed
state as a small prompt directive.
"""

from codey.ghost.directive import GhostDirective, build_ghost_directive
from codey.ghost.hebbian import GhostEdge, GhostHebbianStore, GhostNode
from codey.ghost.inbox import GhostInboxStore, GhostMemoryCandidate
from codey.ghost.learning_loop import GhostLearningLoop, GhostLearningResult, GhostLearningTurn
from codey.ghost.schema import GhostSignal, GhostSignalParseResult
from codey.ghost.store import GhostSignalStore

__all__ = [
    "GhostDirective",
    "GhostEdge",
    "GhostHebbianStore",
    "GhostInboxStore",
    "GhostLearningLoop",
    "GhostLearningResult",
    "GhostLearningTurn",
    "GhostMemoryCandidate",
    "GhostNode",
    "GhostSignal",
    "GhostSignalParseResult",
    "GhostSignalStore",
    "build_ghost_directive",
]
