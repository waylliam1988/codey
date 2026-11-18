"""Isolated native-field probe using production Ghost reads and admission."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from contextlib import closing
from pathlib import Path

from codey.agents.request import AgentRequest
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.observation_index import render_retrieved_block
from codey.ghost.payload_types import decoded_object, mapping_fields
from codey.operations.project_experiences import validated_project_experiences, validated_project_preferences
from codey.operations.project_goals import validated_project_goal_sources
from codey.providers.api_provider import ApiProvider
from codey.providers.base import AssistantTurn, ProviderToolDefinition, TurnFinish
from codey.workspace.context_admission import (
    GhostContextSource,
    admit_ghost_context,
    request_scope,
    sources_from_values,
)
from codey.workspace.revision import WorkspaceRevisionStore
from tests.manual.ghost_memory_routing import portable_context_trial as legacy
from tests.manual.ghost_memory_routing.project_experience_continuity import _models
from tests.manual.ghost_memory_routing.world_model_verification_review import MODEL

PROTOCOL = "native-fields-v4"
TOOL_NAME = "record_context_probe"
TASK = (
    "Recall our project interface choice and its reason, then choose the interface priority and response tone. "
    "Use saved local context if available; otherwise use unknown for choice/reason/priority and neutral for tone. "
    "Call record_context_probe exactly once with the four schema fields directly as arguments. "
    "Do not call done, put data inside summary, inspect files, or call other tools. "
    "Use the exact allowed labels, without explanations. Map warm and considerate to warm. "
    "The listed options are not evidence of a past choice."
)
SOURCE_KEYS = frozenset({"ghost_directive", "ghost_experiences",
                         "ghost_project_goal_history", "ghost_confirmed_project_goal"})
CONFIGS: tuple[tuple[int, bool], ...] = ((8192, False), (8192, True))


def probe_tool() -> ProviderToolDefinition:
    properties: dict[str, object] = {
        key: {"type": "string", "enum": list(values)} for key, values in legacy.FIELD_LABELS.items()
    }
    parameters: dict[str, object] = {"type": "object", "properties": properties,
                                    "required": list(properties), "additionalProperties": False}
    return ProviderToolDefinition(TOOL_NAME, "Record the four evaluation labels; no execution authority.", parameters)


def decode_probe(turn: AssistantTurn) -> dict[str, str]:
    if turn.finish != TurnFinish.COMPLETE or turn.text.strip() or len(turn.tool_calls) != 1:
        raise ValueError("expected one complete native probe call without answer prose")
    call = turn.tool_calls[0]
    if call.name != TOOL_NAME or not call.id or call.arguments.keys() != legacy.FIELD_LABELS.keys():
        raise ValueError("unexpected tool, missing call identity, or invalid probe fields")
    result: dict[str, str] = {}
    for key, value in call.arguments.items():
        if not isinstance(value, str) or value not in legacy.FIELD_LABELS[key]:
            raise ValueError(f"invalid label for {key}")
        result[key] = value
    return result


def read_sources(request: AgentRequest) -> tuple[GhostContextSource, ...]:
    return (*sources_from_values(
        directive=validated_project_preferences(request), experiences=validated_project_experiences(request),
        scope=request_scope(request.project, request.session_id),
    ), *validated_project_goal_sources(request))


def admit_payload(request: AgentRequest, sources: tuple[GhostContextSource, ...],
                  prompt: str, payload: dict[str, object]) -> None:
    if read_sources(request) != sources:
        raise ValueError("local sources changed before physical send")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("missing outbound messages")
    rows: list[object] = messages
    # ApiProvider appends its execution clock as another user message.
    # The admitted prompt must appear exactly once, independent of that clock.
    matches = sum(mapping_fields(row) and row.get("role") == "user" and row.get("content") == prompt for row in rows)
    if matches != 1:
        raise ValueError("outbound prompt differs from admitted snapshot")
    admit_ghost_context(prompt, sources, request_scope=request_scope(request.project, request.session_id),
                        require_all=True, admit_hints=request.admit_hints)


def cell(home: Path, *, output: int, stream: bool, arm: str) -> dict[str, object]:
    if arm not in legacy.ARMS:
        raise ValueError("unsupported arm")
    home.mkdir(parents=True, exist_ok=False)
    project = home / "project"
    project.mkdir()
    state_home = home / "state"
    legacy.seed(state_home, project)
    surface = GhostControlSurface.from_state_home(state_home)
    assert surface.inbox is not None and surface.observations is not None
    enabled = arm != "off"
    if not surface.inbox.set_control("admit_hints", enabled):
        raise ValueError("control update failed")
    task = TASK + (" For this turn explicitly choose animations and a technical tone." if arm == "override" else "")
    rows = surface.observations.retrieve(task, project=str(project), session_id="next-session",
                                         scope="project", exclude_run_id="next")
    with closing(legacy.CapturedProvider(home, output=output, stream=stream)) as provider:
        provider.bind_usage("native-probe", lambda record: None)
        request = AgentRequest(provider=provider, project=project, task=task, session_id="next-session",
            run_id="next", workspace_revision_store=WorkspaceRevisionStore(state_home), admit_hints=enabled,
            ghost_experiences=render_retrieved_block(rows) if rows else "")
        sources = read_sources(request)
        if {source.key for source in sources} != (SOURCE_KEYS if enabled else frozenset()):
            raise ValueError("unexpected local source set")
        prompt = "\n\n".join((task, *(source.text for source in sources)))
        admitted = admit_ghost_context(prompt, sources, request_scope=request_scope(project, "next-session"),
                                       require_all=True, admit_hints=enabled)
        legacy.save(home / "admission.json", admitted.epoch.to_payload())

        def gate(_client: ApiProvider, payload: dict[str, object]) -> None:
            admit_payload(request, sources, prompt, payload)

        provider.bind_attempt_gate(gate)
        started = time.monotonic()
        try:
            turn = provider.send_turn(prompt, [probe_tool()], require_tool_call=True)
            elapsed = (time.monotonic() - started) * 1000
            legacy.save(home / "native-reply.json", {"text": turn.text, "reasoning": turn.reasoning,
                "finish": turn.finish.value, "tool_calls": [
                    {"id": call.id, "name": call.name, "arguments": call.arguments} for call in turn.tool_calls]})
            labels = decode_probe(turn)
            if provider.requests != 1 or len(provider.usage_records) != 1:
                raise ValueError("native probe did not use exactly one physical request and usage observation")
            usage = provider.usage_records[0]
            payload = decoded_object(provider.wire.decode("utf-8"))
            if (payload.get("stream_options") == {"include_usage": True}) != stream:
                raise ValueError("incorrect usage request configuration")
            wire_flags = tuple(text in provider.wire.decode("utf-8") for text in
                               ("tone = warm and considerate", legacy.GOAL, legacy.SEED_REPLY))
            if any(present != enabled for present in wire_flags):
                raise ValueError("local source visibility mismatch")
            metrics: dict[str, object] = {"protocol": PROTOCOL, "output_tokens": output, "stream": stream,
                "arm": arm, "requests": provider.requests, "sources_present": all(wire_flags),
                "elapsed_ms": elapsed, "usage_status": usage.usage_status,
                "input_tokens": usage.usage.input_tokens, "generated_tokens": usage.usage.output_tokens,
                **legacy.score_answer(json.dumps(labels), arm=arm)}
            legacy.save(home / "metrics.json", metrics)
            return metrics
        finally:
            legacy.save(home / "usage.json", [record.to_payload() for record in provider.usage_records])
            legacy.save(home / "attempt.json", {"physical_requests": provider.requests})


def fingerprints() -> dict[str, str]:
    root = Path(__file__).resolve().parents[3]
    paths = [*sorted((root / "codey").rglob("*.py")), root / "codey/runtime.json",
             Path(__file__).resolve(), root / "tests/manual/ghost_memory_routing/portable_context_trial.py"]
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def run_live(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=False)
    frozen = fingerprints()
    legacy.save(home / "sources-before.json", frozen)
    legacy.save(home / "protocol.json", {"protocol": PROTOCOL, "tool": probe_tool().parameters,
        "task": TASK, "configs": CONFIGS, "arms": legacy.ARMS,
        "missing_usage_policy": "unknown; not an interface failure or zero usage"})
    completed: list[dict[str, object]] = []
    try:
        _models(home, "before")
        for output, stream in CONFIGS:
            for arm in legacy.ARMS:
                if fingerprints() != frozen:
                    raise RuntimeError("production or experiment sources changed during the frozen trial")
                row = cell(home / f"output{output}-stream{int(stream)}-{arm}", output=output, stream=stream, arm=arm)
                completed.append(row)
                legacy.save(home / "progress.json", completed)
                print(json.dumps(row), flush=True)
                if fingerprints() != frozen:
                    raise RuntimeError("production or experiment sources changed during the frozen trial")
        _models(home, "after")
        legacy.save(home / "report.json", {"status": "COMPLETE", "protocol": PROTOCOL,
            "model": MODEL, "classification": "ISOLATED_NATIVE_INTERFACE_SMOKE", "cells": completed,
            "ghost_extra_generation_requests": 0, "automatic_retries": 0})
    except Exception as exc:
        legacy.save(home / "failure.json", {"status": "INCOMPLETE", "type": type(exc).__name__,
            "message": str(exc), "completed": len(completed)})
        raise
    finally:
        legacy.save(home / "sources-after.json", fingerprints())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output: object = args.output
    if not isinstance(output, Path):
        raise ValueError("output path required")
    run_live(output)


if __name__ == "__main__":
    main()
