"""Journaled consumer promotion, independent of individual confirmation."""

from __future__ import annotations

import sqlite3

from codey.ghost.storage import load, save
from codey.memory.contracts import MEMORY_FAMILY_STATES, MemoryEvent, MemoryFamilyState
from codey.memory.slots import FAMILIES
from codey.types.json_value import require_json_boolean, require_json_integer, require_json_string, require_json_strings

STATES: dict[str, MemoryFamilyState] = {state: state for state in MEMORY_FAMILY_STATES}
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "observed": ("shadow_candidate", "quarantined", "deleted"),
    "shadow_candidate": ("production_candidate", "demoted", "quarantined", "deleted"),
    "production_candidate": ("enabled", "demoted", "quarantined", "deleted"),
    "enabled": ("demoted", "quarantined", "deleted"),
    "demoted": ("shadow_candidate", "quarantined", "deleted"),
    "quarantined": ("shadow_candidate", "deleted"), "deleted": (),
}


def family_state(connection: sqlite3.Connection, family: str, consumer: str, scope: str) -> MemoryFamilyState:
    row = load(connection, "families", scope + ":" + consumer + ":" + family)
    if row is None:
        # Active projections contain only real experiences or confirmed items.
        # Their declarative consumption is the enabled baseline.
        return "enabled"
    state = STATES.get(require_json_string(row.get("state"), "state"))
    if state is None:
        raise ValueError("invalid stored memory family state")
    return state


def effective_family_state(connection: sqlite3.Connection, family: str, consumer: str,
                           scopes: tuple[str, ...]) -> MemoryFamilyState:
    state: MemoryFamilyState = "enabled"
    for scope in scopes:
        if load(connection, "families", scope + ":" + consumer + ":" + family) is not None:
            state = family_state(connection, family, consumer, scope)
            if state != "enabled":
                return state
    return state


def project_family(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    family = require_json_string(event.payload.get("family"), "family")
    consumer = require_json_string(event.payload.get("consumer"), "consumer")
    target = require_json_string(event.payload.get("state"), "state")
    if family not in FAMILIES or consumer not in {"chat", "project", "research"} or target not in STATES:
        raise ValueError("unknown memory family, consumer or state")
    previous = family_state(connection, family, consumer, event.scope)
    if target not in TRANSITIONS[previous]:
        raise ValueError("invalid memory family transition")
    evaluations = require_json_strings(event.payload.get("evaluation_refs", []), "evaluation_refs")
    if target in {"production_candidate", "enabled"} and not evaluations:
        raise ValueError("promotion requires explicit evaluation references")
    for reference in evaluations:
        report = load(connection, "evaluations", reference)
        if report is None or report.get("family") != family or report.get("consumer") != consumer:
            raise ValueError("evaluation does not belong to this family and consumer")
        if target in {"production_candidate", "enabled"} and (
            require_json_integer(report.get("treatment_passed"), "treatment_passed") <= require_json_integer(report.get("control_passed"), "control_passed")
            or report.get("boundary_violations") != 0 or report.get("costs_accepted") is not True):
            raise ValueError("evaluation does not qualify this family for promotion")
    save(connection, "families", event.scope + ":" + consumer + ":" + family, event.scope,
         {"id": event.scope + ":" + consumer + ":" + family, "scope": event.scope, "family": family,
          "consumer": consumer, "state": target, "previous": previous,
          "source_refs": list(event.provenance), "evaluation_refs": [value for value in evaluations], "at": event.occurred_at})


def project_evaluation(connection: sqlite3.Connection, event: MemoryEvent) -> None:
    report = dict(event.payload)
    identity = require_json_string(report.get("id"), "id")
    if report.get("family") not in FAMILIES or report.get("consumer") not in {"chat", "project", "research"}:
        raise ValueError("unsupported evaluation consumer")
    for key in ("protocol_digest", "dataset_digest", "source_digest"):
        value = require_json_string(report.get(key), key)
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("evaluation requires frozen SHA-256 identities")
    pairs = require_json_integer(report.get("pairs"), "pairs")
    requests = require_json_integer(report.get("requests"), "requests")
    responses = require_json_integer(report.get("responses"), "responses")
    violations = require_json_integer(report.get("boundary_violations"), "boundary_violations")
    require_json_boolean(report.get("costs_accepted"), "costs_accepted")
    if pairs < 1 or requests != pairs * 2 or responses != requests or violations < 0:
        raise ValueError("evaluation batch is incomplete")
    for key in ("control_passed", "treatment_passed"):
        count = require_json_integer(report.get(key), key)
        if not 0 <= count <= pairs:
            raise ValueError("invalid evaluation score")
    previous = load(connection, "evaluations", identity)
    if previous is not None and previous != report:
        raise ValueError("evaluation identity already names different results")
    save(connection, "evaluations", identity, event.scope, report)
