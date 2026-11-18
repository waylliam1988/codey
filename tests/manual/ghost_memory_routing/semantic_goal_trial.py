"""Frozen semantic project-goal consumer experiment; no production imports."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from codey.durable import DurableSession, SQLiteDurableStore
from codey.ghost.payload_types import decoded_object
from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.kernel_contracts import KernelExecutionDeps, KernelObservationDeps
from codey.operations.kernel_request_builder import KernelRunRequestBuilder
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_loop import run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.api_provider import ApiProvider
from codey.providers.base import AssistantTurn, ProviderToolDefinition, TurnFinish
from codey.providers.token_accounting import ApiExchangeUsage
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import require_json_integer, require_json_string
from codey.workspace.context_admission import GhostContextSource, request_scope
from codey.workspace.context_source import ContextSource, render_context_source
from tests.manual.ghost_memory_routing.project_experience_continuity import _models, _save
from tests.manual.ghost_memory_routing.world_model_verification_review import (
    ENDPOINT,
    MODEL,
    SingleAttemptPolicy,
    parse_usage,
)

STREAMS = 20
PHASES = 3
Arm = Literal["none", "retrieval", "confirmed", "forecast"]
ARMS: tuple[Arm, ...] = ("none", "retrieval", "confirmed", "forecast")
Outcome = Literal["hit", "miss", "unjudged"]
CONTEXT = "storage_design"
HISTORY_BUDGET = 3200
GOAL_BUDGET = 600
FORECAST_BUDGET = 900
DOMAINS = ("field notes", "warehouse counts", "travel journals", "garden records", "survey forms",
           "equipment logs", "museum catalogues", "trail reports", "recipe collections", "clinic schedules")


class Priority(StrEnum):
    OFFLINE = "offline_access"
    SHARED = "shared_editing"


def initial(index: int) -> Priority:
    return Priority.OFFLINE if index % 2 == 0 else Priority.SHARED


def opposite(priority: Priority) -> Priority:
    return Priority.SHARED if priority == Priority.OFFLINE else Priority.OFFLINE


def expected(index: int, phase: int) -> Priority:
    return initial(index) if phase == 0 else opposite(initial(index))


def explanation(priority: Priority) -> str:
    return ("working without a network takes priority over simultaneous collaboration"
            if priority == Priority.OFFLINE else
            "simultaneous collaboration takes priority over working without a network")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class Event:
    ref: str
    scope: str
    context: str
    at: int
    priority: Priority
    quote: str
    behavior_ref: str = ""
    behavior_digest: str = ""
    actual_summary: str = ""
    previous_review: Outcome = "unjudged"

    def payload(self) -> dict[str, object]:
        return {"schema": 1, "ref": self.ref, "scope": self.scope, "context": self.context,
                "at": self.at, "priority": self.priority.value, "quote": self.quote,
                "behavior_ref": self.behavior_ref, "behavior_digest": self.behavior_digest,
                "actual_summary": self.actual_summary, "previous_review": self.previous_review}

    @classmethod
    def decode(cls, value: dict[str, object]) -> Event:
        if set(value) != {"schema", "ref", "scope", "context", "at", "priority", "quote", "behavior_ref",
                          "behavior_digest", "actual_summary", "previous_review"}:
            raise ValueError("invalid event fields")
        if type(value["schema"]) is not int or value["schema"] != 1:
            raise ValueError("invalid schema")
        outcome = value["previous_review"]
        if outcome not in ("hit", "miss", "unjudged"):
            raise ValueError("invalid review")
        review: Outcome = "hit" if outcome == "hit" else "miss" if outcome == "miss" else "unjudged"
        row = cls(require_json_string(value["ref"], "ref"), require_json_string(value["scope"], "scope"),
                  require_json_string(value["context"], "context"), require_json_integer(value["at"], "at"),
                  Priority(require_json_string(value["priority"], "priority")),
                  require_json_string(value["quote"], "quote"),
                  require_json_string(value["behavior_ref"], "behavior_ref"),
                  require_json_string(value["behavior_digest"], "behavior_digest"),
                  require_json_string(value["actual_summary"], "actual_summary"), review)
        if (not row.ref or not row.scope or not row.context or not row.quote or row.at < 0
                or (row.behavior_ref and (not row.actual_summary or row.behavior_digest != digest(row.actual_summary)))
                or (not row.behavior_ref and (row.actual_summary or row.behavior_digest))):
            raise ValueError("invalid event identity, time or reply binding")
        return row


def load(path: Path) -> tuple[Event, ...]:
    if not path.exists():
        return ()
    rows = tuple(Event.decode(decoded_object(line)) for line in path.read_text(encoding="utf-8").splitlines())
    if len({row.ref for row in rows}) != len(rows) or any(a.at >= b.at for a, b in zip(rows, rows[1:], strict=False)):
        raise ValueError("duplicate or nonchronological event")
    return rows


def append(path: Path, event: Event) -> None:
    row = Event.decode(event.payload())
    rows = load(path)
    if any(old.ref == row.ref for old in rows) or (rows and rows[-1].at >= row.at):
        raise ValueError("duplicate or nonchronological append")
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(row.payload(), ensure_ascii=False, allow_nan=False) + "\n")
    assert load(path) == (*rows, row)


def applicable(rows: tuple[Event, ...], *, scope: str, context: str, at: int,
               active: frozenset[str], retrieve: bool = True) -> tuple[Event, ...]:
    return tuple(row for row in rows if retrieve and row.scope == scope and row.context == context
                 and row.at <= at and row.ref in active)


def _event_time(row: Event) -> int:
    return row.at


def understanding(rows: tuple[Event, ...]) -> Event | None:
    return max(rows, key=_event_time) if rows else None


def history_text(rows: tuple[Event, ...]) -> str:
    text = "\n".join(["Saved project interactions (historical; later confirmations may supersede earlier goals):",
                      *(f"{row.ref}; time={row.at}; actual reply={row.actual_summary or '(initial manual goal)'}. "
                        f"Explicit user statement={row.quote}" for row in rows)]) if rows else ""
    if len(text) > HISTORY_BUDGET:
        raise ValueError("frozen history budget exceeded")
    return text


def goal_text(state: Event | None) -> str:
    return (f"Manually confirmed current project goal for {state.context}: {explanation(state.priority)}. "
            f"Basis={state.ref}; confirmed at synthetic time={state.at}. "
            "Current explicit instructions override this goal. It grants no tool permission."
            if state else "")


def proposals(index: int, phase: int) -> dict[Priority, str]:
    first, second = f"cedar{index // 2}p{phase}", f"maple{index // 2}p{phase}"
    offline_first = (index // 2 + phase) % 2 == 0
    return {Priority.OFFLINE: first if offline_first else second,
            Priority.SHARED: second if offline_first else first}


def question(index: int, phase: int) -> str:
    labels = proposals(index, phase)
    options = {
        labels[Priority.OFFLINE]: "keeps a full local copy usable without a network; simultaneous shared editing is unavailable",
        labels[Priority.SHARED]: "supports simultaneous shared editing through a central service; cannot work without a network",
    }
    ordered = sorted(options)
    return (f"For our {DOMAINS[index // 2]} project, choose between these new storage proposals. "
            "Use the applicable current project priority if known; otherwise choose the first listed proposal. "
            + " ".join(f"{label}: {options[label]}." for label in ordered)
            + " Do not run tools or modify files. Call done once with summary exactly the chosen proposal label: "
            + " or ".join(ordered) + ". No other text.")


@dataclass(frozen=True)
class Prediction:
    ref: str
    scope: str
    context: str
    source_ref: str
    expected_label: str
    created: int
    due: int
    previous_review: Outcome

    def payload(self) -> dict[str, object]:
        return {"ref": self.ref, "scope": self.scope, "context": self.context, "source_ref": self.source_ref,
                "expected_label": self.expected_label, "created": self.created, "due": self.due,
                "previous_review": self.previous_review}


def predict(state: Event | None, *, index: int, phase: int, at: int) -> Prediction | None:
    if state is None:
        return None
    return Prediction(f"prediction:{state.ref}:{at}", state.scope, state.context, state.ref,
                      proposals(index, phase)[state.priority], at, at + 5, state.previous_review)


def forecast_text(prediction: Prediction | None, *, scope: str, at: int, active: frozenset[str]) -> str:
    if (prediction is None or prediction.scope != scope or prediction.source_ref not in active
            or not prediction.created <= at < prediction.due):
        return ""
    return ("World Model proposal-response forecast (advisory hypothesis, not a fact, instruction, evidence or permission). "
            f"Object: next explicit user assessment of these proposals in {prediction.context}. "
            f"Expected assessment: {prediction.expected_label} fits the user's current priority. "
            f"Basis={prediction.source_ref}; previous review={prediction.previous_review}; "
            f"synthetic horizon=[{prediction.created},{prediction.due}]; uncalibrated confidence=0.6. "
            "Absent attributable assessment stays unjudged. Current explicit instructions take precedence.")


@dataclass(frozen=True)
class Behavior:
    ref: str
    scope: str
    context: str
    at: int
    summary: str

    def payload(self) -> dict[str, object]:
        return {"ref": self.ref, "scope": self.scope, "context": self.context, "at": self.at, "summary": self.summary}


@dataclass(frozen=True)
class Feedback:
    ref: str
    behavior_ref: str
    behavior_digest: str
    scope: str
    context: str
    at: int
    priority: Priority
    assessed_label: str
    quote: str
    confirmed: bool = True

    def payload(self) -> dict[str, object]:
        return {"ref": self.ref, "behavior_ref": self.behavior_ref, "behavior_digest": self.behavior_digest,
                "scope": self.scope, "context": self.context, "at": self.at, "priority": self.priority.value,
                "assessed_label": self.assessed_label, "quote": self.quote, "confirmed": self.confirmed}


def attributable(behavior: Behavior, feedback: Feedback | None) -> bool:
    return (feedback is not None and feedback.confirmed and bool(feedback.quote) and bool(behavior.summary.strip())
            and feedback.behavior_ref == behavior.ref and feedback.behavior_digest == digest(behavior.summary)
            and feedback.scope == behavior.scope and feedback.context == behavior.context
            and behavior.at < feedback.at)


def review(prediction: Prediction | None, behavior: Behavior, feedback: Feedback | None) -> Outcome:
    if prediction is None or feedback is None or not attributable(behavior, feedback):
        return "unjudged"
    if (prediction.scope != behavior.scope or prediction.context != behavior.context
            or not prediction.created <= behavior.at < feedback.at <= prediction.due
            or not feedback.assessed_label):
        return "unjudged"
    return "hit" if prediction.expected_label == feedback.assessed_label else "miss"


def update(behavior: Behavior, feedback: Feedback | None, outcome: Outcome) -> Event | None:
    # Manual confirmation, not forecast correctness, authorizes this local state update.
    if feedback is None or not attributable(behavior, feedback):
        return None
    return Event(feedback.ref, feedback.scope, feedback.context, feedback.at, feedback.priority,
                 feedback.quote, behavior.ref, digest(behavior.summary), behavior.summary, outcome)


def feedback_for(index: int, phase: int, behavior: Behavior) -> Feedback | None:
    if phase == PHASES - 1:
        return None
    priority = opposite(initial(index))
    label = proposals(index, phase)[priority]
    quote = (f"After your reply, I {'change' if phase == 0 else 'maintain'} my project priority: "
             f"{explanation(priority)}. Of these proposals, {label} fits that priority. "
             "I explicitly confirm this as the current goal for future storage decisions.")
    return Feedback(f"feedback:{index}:{phase}", behavior.ref, digest(behavior.summary), behavior.scope,
                    behavior.context, behavior.at + 1, priority, label, quote)


def render_sources(history: str, goal: str, forecast: str, *, scope: str) -> tuple[str, tuple[GhostContextSource, ...]]:
    texts: list[str] = []
    sources: list[GhostContextSource] = []
    for key, value, budget in (("ghost_experiences", history, HISTORY_BUDGET),
                               ("ghost_confirmed_project_goal", goal, GOAL_BUDGET),
                               ("world_model_proposal_response", forecast, FORECAST_BUDGET)):
        def load_value(text: str = value) -> str:
            return text

        source = render_context_source(ContextSource(key, load_value, budget, "synthetic_clock_snapshot",
                                                     "frozen_semantic_goal_trial", scope=scope))
        if source is not None:
            if source.truncated:
                raise ValueError("frozen source must not truncate")
            texts.append(source.text)
            sources.append(GhostContextSource(key, source.text, budget, scope))
    return "\n\n".join(texts), tuple(sources)


class CapturedProvider(ApiProvider):
    def __init__(self, home: Path) -> None:
        self.home, self.requests, self.wire, self.complete = home, 0, b"", False
        super().__init__(ENDPOINT, MODEL, temperature=0, native_tools=True, timeout=120,
                         context_window_tokens=262144, context_reserve_tokens=32768,
                         context_keep_recent_tokens=32000, output_tokens=8192,
                         usage_parser=parse_usage, request_policy=SingleAttemptPolicy())

    def _observe_http_attempt(self, *, attempt: int, data: bytes, phase: str,
                              response_bytes: int, seconds: float) -> None:
        if phase == "request":
            self.requests += 1
            self.wire = data
            (self.home / f"request-{self.requests}.json").write_bytes(data)

    def send_turn(self, prompt: str, tools: list[ProviderToolDefinition] | None = None,
                  timeout: float | None = None, *, tool_call_observations: str = "",
                  tool_call_history_groups: int = 0, output_token_limit: int = 0,
                  require_tool_call: bool = False) -> AssistantTurn:
        turn = super().send_turn(prompt, tools, timeout, tool_call_observations=tool_call_observations,
                                 tool_call_history_groups=tool_call_history_groups,
                                 output_token_limit=output_token_limit, require_tool_call=require_tool_call)
        self.complete = turn.finish == TurnFinish.COMPLETE
        _save(self.home / "reply.json", {"text": turn.text, "reasoning": turn.reasoning,
                                        "finish": turn.finish.value, "raw": dict(turn.raw)})
        return turn


def cell(home: Path, *, index: int, phase: int, arm: Arm, history: str, goal: str,
         forecast: str, scope: str) -> tuple[dict[str, object], str]:
    home.mkdir(parents=True, exist_ok=False)
    preparation_start = time.monotonic()
    context, sources = render_sources(history if arm != "none" else "",
                                      goal if arm in ("confirmed", "forecast") else "",
                                      forecast if arm == "forecast" else "", scope=scope)
    provider = CapturedProvider(home)
    usage: list[ApiExchangeUsage] = []
    provider.bind_usage(f"semantic-{index}-{phase}-{arm}", usage.append)
    task = question(index, phase)
    durable = DurableSession(SQLiteDurableStore(home / "runtime.sqlite3"))
    durable.admit_input(conversation_id="s", request_id="input", task_id="r", text=task, provider_snapshot={})
    durable.claim_task("r")
    durable.start_task("r", state=dict(durable.task("r").state))
    mutations = RuntimeMutationLine(durable=durable)
    mutations.accept_operation(session_id="s", run_id="r", provider_id="local", turn_budget=1,
                               max_repair_rounds=0, task_kind="chat")
    mutations.mark_writer_running("s", "r", provider_id="local")
    sink = KernelEffectSink(mutations, session_id="s", run_id="r", provider_id="local", durable=durable,
                            managed_outputs=ManagedOutputStore(home))
    session = TaskSession(TaskPolicy(frozenset({"control"})), task_kind="chat", max_turns=1, task_text=task)
    preparation_ms = (time.monotonic() - preparation_start) * 1000
    started = time.monotonic()
    try:
        result = run_task_kernel(session, request=KernelRunRequestBuilder(
            provider, "r", "trial", "local", task, context_text=context, context_scope=scope, ghost_sources=sources,
        ).build(execution=KernelExecutionDeps(intent_sink=sink), observation=KernelObservationDeps(
            propagate_provider_failure=True,
            on_boundary=durable_generation_boundary_callback(durable=durable, session_id="s", task_id="r", sink=sink),
        )))
        elapsed = (time.monotonic() - started) * 1000
        _save(home / "result.json", {"summary": result.summary, "stop_reason": result.stop_reason,
                                     "usage": [row.to_payload() for row in usage]})
        if provider.requests != 1 or len(usage) != 1 or not provider.complete or not result.summary.strip():
            raise ValueError("incomplete response; never retry")
        reported = usage[0].usage
        if reported.input_tokens is None or reported.output_tokens is None:
            raise ValueError("missing service usage; never retry")
        wire = provider.wire.decode()
        assert ("Saved project interactions" in wire) == (arm != "none" and bool(history))
        assert ("Manually confirmed current project goal" in wire) == (arm in ("confirmed", "forecast") and bool(goal))
        assert ("World Model proposal-response forecast" in wire) == (arm == "forecast" and bool(forecast))
        return {"stream": index, "phase": phase, "arm": arm,
                "correct": result.summary.strip() == proposals(index, phase)[expected(index, phase)],
                "valid_reply": result.summary.strip() in proposals(index, phase).values(),
                "input_tokens": reported.input_tokens, "output_tokens": reported.output_tokens,
                "requests": provider.requests, "elapsed_ms": elapsed, "visible_ready_ms": elapsed,
                "preparation_ms": preparation_ms, "wire_bytes": len(provider.wire),
                "wire_sha256": digest(wire)}, result.summary
    finally:
        provider.close()
        durable.close()


def number(row: dict[str, object], key: str) -> float:
    value = row[key]
    if not isinstance(value, float):
        raise ValueError("invalid latency")
    return value


def summarize(rows: list[dict[str, object]], reviews: list[dict[str, object]], sizes: list[int]) -> dict[str, object]:
    keys = {(i, phase, arm) for i in range(STREAMS) for phase in range(PHASES) for arm in ARMS}
    if len(rows) != len(keys) or {(row["stream"], row["phase"], row["arm"]) for row in rows} != keys:
        raise ValueError("missing or duplicate frozen cells")
    review_keys = {(i, phase) for i in range(STREAMS) for phase in range(PHASES)}
    if len(reviews) != len(review_keys) or {(row["stream"], row["phase"]) for row in reviews} != review_keys:
        raise ValueError("missing or duplicate reviews")
    complete = {arm: sum(all(row["correct"] is True for row in rows if row["arm"] == arm and row["stream"] == i)
                         for i in range(STREAMS)) for arm in ARMS}
    by_phase = {arm: [sum(row["correct"] is True for row in rows if row["arm"] == arm and row["phase"] == phase)
                      for phase in range(PHASES)] for arm in ARMS}
    by_initial = {arm: {priority.value: sum(all(row["correct"] is True for row in rows
                                              if row["arm"] == arm and row["stream"] == i)
                                          for i in range(STREAMS) if initial(i) == priority)
                       for priority in Priority} for arm in ARMS}
    p95 = {arm: sorted(number(row, "elapsed_ms") for row in rows if row["arm"] == arm)
           [math.ceil(STREAMS * PHASES * .95) - 1] for arm in ARMS}
    tokens = {(require_json_integer(row["stream"], "stream"), require_json_integer(row["phase"], "phase"), row["arm"]):
              require_json_integer(row["input_tokens"], "input_tokens") for row in rows}
    goal_delta = max(tokens[i, phase, "confirmed"] - tokens[i, phase, "retrieval"]
                     for i in range(STREAMS) for phase in range(PHASES))
    forecast_delta = max(tokens[i, phase, "forecast"] - tokens[i, phase, "confirmed"]
                         for i in range(STREAMS) for phase in range(PHASES))

    def balanced(arm: Arm) -> bool:
        return (complete[arm] >= 18 and min(by_phase[arm]) >= 18 and min(by_initial[arm].values()) >= 9)

    state_cost = len(sizes) == STREAMS and max(sizes) <= 65536
    goal_benefit = balanced("confirmed") and complete["confirmed"] - complete["none"] >= 8 and complete["confirmed"] >= complete["retrieval"]
    forecast_benefit = (balanced("forecast") and complete["forecast"] - complete["confirmed"] >= 4
                        and complete["forecast"] - complete["retrieval"] >= 4
                        and complete["forecast"] - complete["none"] >= 8)
    goal_cost = state_cost and goal_delta <= 250 and p95["confirmed"] <= p95["retrieval"] * 2 + 1000
    forecast_cost = state_cost and forecast_delta <= 400 and p95["forecast"] <= p95["confirmed"] * 2 + 1000
    return {"status": "EXPERIMENT_COMPLETE", "production_decision": "EXPERIMENT_ONLY", "model": MODEL,
            "independent_synthetic_streams": STREAMS, "physical_requests": len(rows),
            "all_three_correct_streams": complete, "phase_correct": by_phase, "streams_by_initial": by_initial,
            "correct_cells": {arm: sum(row["correct"] is True for row in rows if row["arm"] == arm) for arm in ARMS},
            "valid_cells": {arm: sum(row["valid_reply"] is True for row in rows if row["arm"] == arm) for arm in ARMS},
            "p95_ms": p95, "max_goal_extra_input_tokens": goal_delta, "max_forecast_extra_input_tokens": forecast_delta,
            "max_derived_state_bytes": max(sizes), "goal_benefit_gate": goal_benefit, "goal_cost_gate": goal_cost,
            "forecast_benefit_gate": forecast_benefit, "forecast_cost_gate": forecast_cost,
            "goal_eligible_for_production_boundaries": goal_benefit and goal_cost,
            "forecast_eligible_for_production_boundaries": forecast_benefit and forecast_cost,
            "cells": rows, "reviews": reviews}


def run(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=False)
    rows: list[dict[str, object]] = []
    reviews: list[dict[str, object]] = []
    sizes: list[int] = []
    try:
        _models(home, "before")
        for index in range(STREAMS):
            stream_home = home / f"stream-{index:02d}"
            stream_home.mkdir()
            path = stream_home / "events.jsonl"
            scope = request_scope(stream_home, f"user-{index}")
            append(path, Event(f"manual:{index}", scope, CONTEXT, 0, initial(index),
                               f"For this project's storage design, {explanation(initial(index))}. "
                               "I explicitly confirm this as the current goal."))
            for phase in range(PHASES):
                started = time.monotonic()
                saved = load(path)
                active = frozenset(row.ref for row in saved)
                at = 10 * (phase + 1)
                eligible = applicable(saved, scope=scope, context=CONTEXT, at=at, active=active)
                state = understanding(eligible)
                prediction = predict(state, index=index, phase=phase, at=at)
                history, goal = history_text(eligible), goal_text(state)
                forecast = forecast_text(prediction, scope=scope, at=at, active=active)
                state_ms = (time.monotonic() - started) * 1000
                rotation = (index + phase) % len(ARMS)
                actual_summary = ""
                for arm in (*ARMS[rotation:], *ARMS[:rotation]):
                    metric, summary = cell(stream_home / f"phase-{phase}" / arm, index=index, phase=phase,
                                           arm=arm, history=history, goal=goal, forecast=forecast, scope=scope)
                    metric["shared_state_preparation_ms"] = state_ms
                    rows.append(metric)
                    if arm == "forecast":
                        actual_summary = summary
                    _save(home / "completed.json", rows)
                    print(f"stream={index} phase={phase} arm={arm} correct={metric['correct']}", flush=True)
                behavior = Behavior(f"behavior:{index}:{phase}", scope, CONTEXT, at, actual_summary)
                feedback = feedback_for(index, phase, behavior)
                outcome = review(prediction, behavior, feedback)
                reviews.append({"stream": index, "phase": phase, "outcome": outcome})
                _save(stream_home / f"review-{phase}.json", {
                    "prediction": prediction.payload() if prediction else None, "behavior": behavior.payload(),
                    "feedback": feedback.payload() if feedback else None, "outcome": outcome})
                revised = update(behavior, feedback, outcome)
                if revised is not None:
                    append(path, revised)
            sizes.append(path.stat().st_size)
        _models(home, "after")
        _save(home / "metrics.json", summarize(rows, reviews, sizes))
    except Exception as exc:
        _save(home / "failure.json", {"status": "INCOMPLETE", "completed_cells": len(rows),
                                      "error_type": type(exc).__name__, "error": str(exc)})
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
        raise ValueError("explicit --live required")
    run(options.output)


if __name__ == "__main__":
    main()
