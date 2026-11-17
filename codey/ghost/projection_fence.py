"""Keep one deletion generation across a projection and nested maintenance."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import wraps
from typing import Concatenate, ParamSpec, Protocol, TypeVar

from codey.ghost.payload_types import mapping_fields
from codey.ghost.sqlite import GhostSQLite
from codey.runs.ledger_projection import RunLedgerProjection


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
        with owner.database.projection_inputs(run_ids=_run_identities(kwargs)):
            return function(owner, *args, **kwargs)

    return fenced


def _run_identities(arguments: Mapping[str, object]) -> tuple[str, ...]:
    identities: set[str] = set()
    run_id = arguments.get("run_id")
    focus = arguments.get("user_focus_excerpt")
    # Some refresh callers use run_id only as audit metadata while reading
    # independent knowledge. It is an input source only with task content;
    # ledger and terminal sources carry their own identities below.
    if isinstance(run_id, str) and run_id and isinstance(focus, str) and focus.strip():
        identities.add(run_id)
    projection = arguments.get("run_projection")
    if isinstance(projection, RunLedgerProjection) and projection.run_id:
        identities.add(projection.run_id)
    event = arguments.get("terminal_event")
    if mapping_fields(event):
        event_run = event.get("run_id")
        if isinstance(event_run, str) and event_run:
            identities.add(event_run)
    return tuple(sorted(identities))
