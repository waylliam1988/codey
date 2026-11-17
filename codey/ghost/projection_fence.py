"""Keep one deletion generation across a projection and nested maintenance."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Concatenate, ParamSpec, Protocol, TypeVar

from codey.ghost.sqlite import GhostSQLite


class ProjectionOwner(Protocol):
    database: GhostSQLite


Owner = TypeVar("Owner", bound=ProjectionOwner)
Parameters = ParamSpec("Parameters")
Result = TypeVar("Result")


def fence_projection(
    function: Callable[Concatenate[Owner, Parameters], Result],
) -> Callable[Concatenate[Owner, Parameters], Result]:
    @wraps(function)
    def fenced(owner: Owner, /, *args: Parameters.args, **kwargs: Parameters.kwargs) -> Result:
        with owner.database.projection_inputs():
            return function(owner, *args, **kwargs)

    return fenced
