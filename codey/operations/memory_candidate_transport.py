"""Offer optional metadata through the existing frozen completion contract."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from codey.memory.candidates import CANDIDATE_FIELD, candidate_schema
from codey.toolchain.tool_spec import freeze_spec_parameters

if TYPE_CHECKING:
    from codey.operations.kernel_protocol import TurnSnapshot


def offer_memory_candidates(snapshot: TurnSnapshot) -> TurnSnapshot:
    from codey.operations.kernel_protocol import refresh_snapshot_contract

    specs = []
    for spec in snapshot.frozen_specs:
        if spec.name == "done" and CANDIDATE_FIELD not in dict(spec.parameters):
            spec = freeze_spec_parameters(replace(spec,
                parameters=(*spec.parameters, (CANDIDATE_FIELD, candidate_schema())),
                description=spec.description + " Optional memory_candidates may propose durable preferences, goals or hypotheses. "
                "Quote only this request's original user task, using exact zero-based Unicode character offsets. "
                "Use canonical slots response.language, response.detail or code.language when applicable. "
                "related_item_ids may name existing memories that this statement corrects or limits; those relationships remain pending review. "
                "When the user explicitly asks to remember a lasting preference or goal, include a candidate alongside summary; "
                "a summary promising to remember is not saved memory. Candidates always await user confirmation. "
                "For the original task 'I prefer Rust.' the candidate is "
                "{\"kind\":\"preference\",\"text\":\"Use Rust examples\",\"quote\":\"I prefer Rust.\","
                "\"quote_start\":0,\"quote_end\":14,\"slot\":\"code.language\",\"value\":\"rust\"}. "
                "Compute offsets for the actual task; do not copy this example. "
                "For one-answer-only instructions omit memory_candidates. "
                "Omit uncertain candidates; never shorten the answer for metadata."))
        specs.append(spec)
    return refresh_snapshot_contract(replace(snapshot, frozen_specs=tuple(specs)))
