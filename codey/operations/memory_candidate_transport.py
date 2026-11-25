"""Offer optional metadata through the existing frozen completion contract."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from codey.memory.candidates import CANDIDATE_FIELD, candidate_schema
from codey.toolchain.tool_spec import freeze_spec_parameters

if TYPE_CHECKING:
    from codey.operations.kernel.turn_contract import TurnSnapshot
    from codey.toolchain.tool_spec import ToolSpec


def offer_memory_candidates(snapshot: TurnSnapshot) -> TurnSnapshot:
    from codey.operations.kernel.turn_presentation import refresh_snapshot_contract

    specs = tuple(memory_candidate_spec(spec) for spec in snapshot.frozen_specs)
    return refresh_snapshot_contract(replace(snapshot, frozen_specs=specs))


def memory_candidate_spec(spec: ToolSpec) -> ToolSpec:
    """The same optional schema is used for exposure and strict restoration."""
    if spec.name != "done" or CANDIDATE_FIELD in dict(spec.parameters):
        return spec
    return freeze_spec_parameters(replace(spec,
        parameters=(*spec.parameters, (CANDIDATE_FIELD, candidate_schema())),
        description=spec.description + " Optional memory_candidates may propose durable preferences, goals or hypotheses. "
        "Quote a unique, exact substring of this request's original user task. The host locates it. "
        "Use canonical slots response.language, response.detail or code.language when applicable. "
        "related_item_ids may name existing memories that this statement corrects or limits; those relationships remain pending review. "
        "When the user explicitly asks to remember a lasting preference or goal, include a candidate alongside summary; "
        "a summary promising to remember is not saved memory. Candidates always await user confirmation. "
        "For the original task 'I prefer Rust.' the candidate is "
        "{\"kind\":\"preference\",\"text\":\"Use Rust examples\",\"quote\":\"I prefer Rust.\","
        "\"slot\":\"code.language\",\"value\":\"rust\"}. "
        "For one-answer-only instructions omit memory_candidates. "
        "Omit uncertain candidates; never shorten the answer for metadata."))
