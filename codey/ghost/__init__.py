"""Ghost continuity primitives for Codey.

The Ghost layer can project explicit learning signals into an inbox, reinforce
accepted candidates into bounded local Hebbian state, and render confirmed
state as a small prompt directive.
"""

from codey.ghost.continuity import (
    GhostContinuity,
    GhostContinuityItem,
    GhostContinuityResult,
    GhostContinuityStore,
    build_ghost_continuity,
)
from codey.ghost.directive import GhostDirective, build_ghost_directive
from codey.ghost.hebbian import GhostEdge, GhostHebbianStore, GhostNode
from codey.ghost.inbox import GhostInboxStore, GhostMemoryCandidate
from codey.ghost.learning_loop import GhostLearningLoop, GhostLearningResult, GhostLearningTurn
from codey.ghost.router import (
    GhostRouteDecision,
    GhostRouteRequest,
    GhostRouteResult,
    GhostRouteStore,
    GhostRouter,
)
from codey.ghost.schema import GhostSignal, GhostSignalParseResult
from codey.ghost.sleep import (
    GhostSleepBudget,
    GhostSleepCursor,
    GhostSleepReport,
    GhostSleepStepResult,
    GhostSleepStore,
)
from codey.ghost.store import GhostSignalStore

__all__ = [
    "GhostDirective",
    "GhostContinuity",
    "GhostContinuityItem",
    "GhostContinuityResult",
    "GhostContinuityStore",
    "GhostEdge",
    "GhostHebbianStore",
    "GhostInboxStore",
    "GhostLearningLoop",
    "GhostLearningResult",
    "GhostLearningTurn",
    "GhostMemoryCandidate",
    "GhostNode",
    "GhostRouteDecision",
    "GhostRouteRequest",
    "GhostRouteResult",
    "GhostRouteStore",
    "GhostRouter",
    "GhostSignal",
    "GhostSignalParseResult",
    "GhostSignalStore",
    "GhostSleepBudget",
    "GhostSleepCursor",
    "GhostSleepReport",
    "GhostSleepStepResult",
    "GhostSleepStore",
    "build_ghost_continuity",
    "build_ghost_directive",
]
