"""Frozen cross-session consumer trial; production never imports this harness."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from codey.agents.request import AgentRequest
from codey.durable import DurableSession, SQLiteDurableStore
from codey.ghost.observation_index import render_retrieved_block
from codey.ghost.observations import GhostObservationStore
from codey.ghost.payload_types import object_fields
from codey.operations import project_adapter
from codey.policies.permissions import PERMISSION_PROFILES
from codey.policies.task_policy import TaskPolicy
from codey.providers.api_provider import ApiProvider
from codey.providers.base import AssistantTurn, ProviderToolDefinition, TurnFinish
from codey.providers.token_accounting import ApiExchangeUsage
from codey.runtime.observe.events import RunEvent, render_run_event
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore
from codey.workspace.context_admission import GhostContextSource, request_scope, sources_from_values
from tests.manual.ghost_memory_routing.world_model_verification_review import (
    ENDPOINT,
    MODEL,
    SingleAttemptPolicy,
    parse_usage,
)

QUESTION = (
    "For sharedplan, remind me which of borosql or lumenlog we selected and its recorded reason. "
    "Reply exactly <choice> | <recorded reason>. If project history does not establish both, reply UNKNOWN. "
    "Do not run tools or modify files."
)
REASONS = ("offline durability", "bounded memory", "local backups", "simple recovery", "append throughput")
SAMPLES = 20


def fixture(index: int) -> tuple[str, str, str]:
    choice = "borosql" if index % 2 == 0 else "lumenlog"
    reason = REASONS[(index // 2) % len(REASONS)]
    user = (f"sharedplan stream {index}: choose borosql or lumenlog. " + "Requirements remain unchanged. " * 110)[:3000]
    assistant = f"{choice} | {reason}. Decision recorded; validation remains pending."
    return user, assistant, f"{choice} | {reason}"


def correct(summary: str, expected: str) -> bool:
    return summary.strip() == expected


def candidate_sources(request: AgentRequest) -> tuple[GhostContextSource, ...]:
    if not request.admit_hints:
        return ()
    return sources_from_values(
        directive=request.ghost_directive, continuity=request.ghost_continuity,
        experiences=request.ghost_experiences, scope=request_scope(request.project, request.session_id),
    )


def _save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


class CapturedProvider(ApiProvider):
    def __init__(self, home: Path) -> None:
        self.home = home
        self.requests = 0
        self.wire = b""
        self.complete = False
        super().__init__(
            ENDPOINT, MODEL, temperature=0, native_tools=True, timeout=120,
            context_window_tokens=262144, context_reserve_tokens=32768,
            context_keep_recent_tokens=32000, output_tokens=128,
            usage_parser=parse_usage, request_policy=SingleAttemptPolicy(),
        )

    def _observe_http_attempt(
        self, *, attempt: int, data: bytes, phase: str, response_bytes: int, seconds: float,
    ) -> None:
        if phase == "request":
            self.requests += 1
            self.wire = data
            (self.home / f"request-{self.requests}.json").write_bytes(data)

    def send_turn(
        self, prompt: str, tools: list[ProviderToolDefinition] | None = None,
        timeout: float | None = None, *, tool_call_observations: str = "",
        tool_call_history_groups: int = 0, output_token_limit: int = 0,
        require_tool_call: bool = False,
    ) -> AssistantTurn:
        turn = super().send_turn(
            prompt, tools, timeout, tool_call_observations=tool_call_observations,
            tool_call_history_groups=tool_call_history_groups, output_token_limit=output_token_limit,
            require_tool_call=require_tool_call,
        )
        self.complete = turn.finish == TurnFinish.COMPLETE
        raw: dict[str, object] = {
            "text": turn.text, "reasoning": turn.reasoning, "finish": turn.finish.value,
            "raw": dict(turn.raw),
        }
        _save(self.home / "reply.json", raw)
        return turn


def _models(home: Path, name: str) -> None:
    with urlopen(ENDPOINT + "/models", timeout=5) as response:
        raw = response.read()
    (home / f"models-{name}.json").write_bytes(raw)
    value: object = json.loads(raw)
    if not object_fields(value):
        raise ValueError("invalid model metadata")
    data = value.get("data")
    if not isinstance(data, list):
        raise ValueError("missing model metadata")
    rows: list[object] = data
    if len(rows) != 1 or not object_fields(rows[0]) or rows[0].get("id") != MODEL:
        raise ValueError("target model mismatch")


def _p95(values: list[float]) -> float:
    return sorted(values)[math.ceil(len(values) * .95) - 1]


def _cell(home: Path, index: int, enabled: bool) -> dict[str, object]:
    home.mkdir(parents=True)
    preparation_start = time.monotonic()
    user, assistant, expected = fixture(index)
    project = home / "project"
    project.mkdir()
    history = home / "history"
    store = GhostObservationStore(history)
    assert store.vector_memory is None, "this trial forbids embedding requests"
    assert store.append_completed(
        run_id=f"source-{index}", session_id="old-session", project=str(project), mode="project",
        user_text=user, assistant_text=assistant, stop_reason="done", provider_id="synthetic-history",
    )
    store = GhostObservationStore(history)
    picked = store.retrieve(QUESTION, session_id="new-session", project=str(project), scope="project",
                            max_items=3, budget_chars=1800)
    experiences = render_retrieved_block(picked, 1800)
    assert assistant in experiences and len(experiences) <= 1800
    durable = DurableSession(SQLiteDurableStore(home / "runtime.sqlite3"))
    durable.admit_input(conversation_id="new-session", request_id="next", task_id="next",
                        text=QUESTION, provider_snapshot={})
    durable.claim_task("next")
    durable.start_task("next", state=dict(durable.task("next").state))
    mutations = RuntimeMutationLine(durable=durable)
    mutations.accept_operation(session_id="new-session", run_id="next", provider_id="trial",
                               turn_budget=1, max_repair_rounds=0, task_kind="project")
    mutations.mark_writer_running("new-session", "next", provider_id="trial")
    outputs = ManagedOutputStore(home)
    preparation_ms = (time.monotonic() - preparation_start) * 1000
    provider = CapturedProvider(home)
    usage: list[ApiExchangeUsage] = []

    def retain_usage(row: ApiExchangeUsage) -> None:
        usage.append(row)

    events: list[str] = []

    def retain_event(event: RunEvent) -> None:
        events.append(render_run_event(event))

    provider.bind_usage(f"stream-{index}-{enabled}", retain_usage)
    started = time.monotonic()
    try:
        result = project_adapter.run(AgentRequest(
            provider=provider, project=project, task=QUESTION, max_turns=1,
            task_policy=TaskPolicy(grants=frozenset({"control"})),
            session_id="new-session", ghost_experiences=experiences, admit_hints=enabled,
            run_id="next", provider_id="trial", durable=durable,
            runtime_mutations=mutations, managed_outputs=outputs,
            on_event=retain_event,
        ))
        elapsed_ms = (time.monotonic() - started) * 1000
        raw_result: dict[str, object] = {
            "summary": result.summary, "stop_reason": result.stop_reason, "events": events,
            "usage": [row.to_payload() for row in usage],
        }
        _save(home / "result.json", raw_result)
        if provider.requests != 1 or len(usage) != 1 or not provider.complete or not result.summary.strip():
            raise ValueError("incomplete response or request count mismatch")
        reported = usage[0].usage
        if reported.input_tokens is None or reported.output_tokens is None:
            raise ValueError("missing service usage")
        wire = provider.wire.decode()
        assert ("Related past experiences" in wire) == enabled
        assert (assistant in wire) == enabled
        return {
            "stream": index, "enabled": enabled, "correct": correct(result.summary, expected),
            "choice": "borosql" if index % 2 == 0 else "lumenlog",
            "input_tokens": reported.input_tokens, "output_tokens": reported.output_tokens,
            "requests": provider.requests, "elapsed_ms": elapsed_ms, "visible_ready_ms": elapsed_ms,
            "preparation_ms": preparation_ms, "wire_bytes": len(provider.wire),
            "wire_sha256": hashlib.sha256(provider.wire).hexdigest(),
        }
    finally:
        provider.close()
        durable.close()


def summarize(rows: list[dict[str, object]]) -> dict[str, object]:
    enabled = [row for row in rows if row["enabled"] is True]
    disabled = [row for row in rows if row["enabled"] is False]
    assert len(enabled) == len(disabled) == SAMPLES
    on = sum(row["correct"] is True for row in enabled)
    off = sum(row["correct"] is True for row in disabled)
    choice_counts = {choice: sum(row["correct"] is True for row in enabled if row["choice"] == choice)
                     for choice in ("borosql", "lumenlog")}
    delta_tokens: list[int] = []
    latencies: dict[bool, list[float]] = {True: [], False: []}
    for index in range(SAMPLES):
        pair = [row for row in rows if row["stream"] == index]
        a = next(row for row in pair if row["enabled"] is True)
        b = next(row for row in pair if row["enabled"] is False)
        a_tokens, b_tokens = a["input_tokens"], b["input_tokens"]
        assert type(a_tokens) is int and type(b_tokens) is int
        delta_tokens.append(a_tokens - b_tokens)
    for row in rows:
        seconds, arm = row["elapsed_ms"], row["enabled"]
        assert isinstance(seconds, float) and type(arm) is bool
        latencies[arm].append(seconds)
    benefit = on >= 18 and on - off >= 8 and all(count >= 9 for count in choice_counts.values())
    cost = max(delta_tokens) <= 600 and _p95(latencies[True]) <= _p95(latencies[False]) * 2 + 1000
    return {
        "status": "EXPERIMENT_COMPLETE", "model": MODEL, "streams": SAMPLES, "physical_requests": len(rows),
        "on_correct": on, "off_correct": off, "on_choice_correct": choice_counts,
        "max_extra_input_tokens": max(delta_tokens),
        "on_p95_ms": _p95(latencies[True]), "off_p95_ms": _p95(latencies[False]),
        "benefit_gate": benefit, "cost_gate": cost, "eligible_for_boundary_validation": benefit and cost,
        "production_decision": "EXPERIMENT_ONLY", "cells": rows,
    }


def run(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=False)
    rows: list[dict[str, object]] = []
    try:
        _models(home, "before")
        profiles = {name: replace(profile, context_sources=(*profile.context_sources, "ghost_experiences"))
                    if name == "coding_writer" else profile for name, profile in PERMISSION_PROFILES.items()}
        with patch.dict(PERMISSION_PROFILES, profiles), patch.object(project_adapter, "_project_ghost_sources", candidate_sources):
            for index in range(SAMPLES):
                for enabled in ((False, True) if index % 2 == 0 else (True, False)):
                    row = _cell(home / f"stream-{index:02d}" / ("on" if enabled else "off"), index, enabled)
                    rows.append(row)
                    _save(home / "completed.json", rows)
                    print(f"stream={index} enabled={enabled} correct={row['correct']}", flush=True)
        _models(home, "after")
        _save(home / "metrics.json", summarize(rows))
    except Exception as exc:
        failure: dict[str, object] = {"status": "INCOMPLETE", "completed": len(rows),
                                      "error_type": type(exc).__name__, "error": str(exc)}
        _save(home / "failure.json", failure)
        raise


class Options(argparse.Namespace):
    output: Path
    live: bool


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    options = parser.parse_args(namespace=Options())
    if not options.live:
        raise ValueError("explicit --live is required")
    run(options.output)


if __name__ == "__main__":
    main()
