"""Frozen personal feedback/World Model trial; never imported by production."""

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
from codey.providers.token_accounting import ApiExchangeUsage
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import require_json_integer, require_json_string
from codey.workspace.context_admission import GhostContextSource, request_scope
from codey.workspace.context_source import ContextSource, render_context_source
from tests.manual.ghost_memory_routing.project_experience_continuity import CapturedProvider, _models, _save

STREAMS = 20
PHASES = 5
ARMS = ("none", "retrieval", "forecast")
HISTORY_BUDGET = 2400
FORECAST_BUDGET = 900
PLAN = "architecture_plan"
Arm = Literal["none", "retrieval", "forecast"]
Outcome = Literal["hit", "miss", "unjudged"]


class Opening(StrEnum):
    COMPARE = "compare_first"
    RECOMMEND = "recommend_first"


def opposite(value: Opening) -> Opening:
    return Opening.RECOMMEND if value == Opening.COMPARE else Opening.COMPARE


def initial(index: int) -> Opening:
    return Opening.COMPARE if index % 2 == 0 else Opening.RECOMMEND


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class Experience:
    ref: str
    scope: str
    context: str
    at: int
    until: int | None
    desired: Opening
    quote: str
    behavior_ref: str = ""
    behavior_digest: str = ""
    actual_summary: str = ""
    previous_review: Outcome = "unjudged"

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1, "ref": self.ref, "scope": self.scope, "context": self.context,
            "at": self.at, "until": self.until, "desired": self.desired.value, "quote": self.quote,
            "behavior_ref": self.behavior_ref, "behavior_digest": self.behavior_digest,
            "actual_summary": self.actual_summary, "previous_review": self.previous_review,
        }

    @classmethod
    def from_payload(cls, value: dict[str, object]) -> Experience:
        keys = {"schema_version", "ref", "scope", "context", "at", "until", "desired", "quote",
                "behavior_ref", "behavior_digest", "actual_summary", "previous_review"}
        if set(value) != keys or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("invalid experience schema")
        until = value["until"]
        if until is not None:
            until = require_json_integer(until, "until")
        at = require_json_integer(value["at"], "at")
        review = value["previous_review"]
        if review not in ("hit", "miss", "unjudged"):
            raise ValueError("invalid review outcome")
        outcome: Outcome = "hit" if review == "hit" else "miss" if review == "miss" else "unjudged"
        row = cls(
            require_json_string(value["ref"], "ref"), require_json_string(value["scope"], "scope"),
            require_json_string(value["context"], "context"), at, until,
            Opening(require_json_string(value["desired"], "desired")),
            require_json_string(value["quote"], "quote"),
            require_json_string(value["behavior_ref"], "behavior_ref"),
            require_json_string(value["behavior_digest"], "behavior_digest"),
            require_json_string(value["actual_summary"], "actual_summary"), outcome,
        )
        if (not row.ref or not row.scope or not row.context or not row.quote or at < 0
                or (until is not None and until <= at)
                or (row.behavior_ref and row.behavior_digest != digest(row.actual_summary))
                or (not row.behavior_ref and (row.behavior_digest or row.actual_summary))):
            raise ValueError("invalid experience identity, time or behavior binding")
        return row


def load(path: Path) -> tuple[Experience, ...]:
    if not path.exists():
        return ()
    rows = tuple(Experience.from_payload(decoded_object(line)) for line in path.read_text(encoding="utf-8").splitlines())
    if len({row.ref for row in rows}) != len(rows) or any(a.at >= b.at for a, b in zip(rows, rows[1:], strict=False)):
        raise ValueError("duplicate or nonchronological experience")
    return rows


def append(path: Path, experience: Experience) -> None:
    # Revalidate producer objects at the same boundary as cold replay.
    row = Experience.from_payload(experience.to_payload())
    prior = load(path)
    if any(old.ref == row.ref for old in prior) or (prior and prior[-1].at >= row.at):
        raise ValueError("duplicate or nonchronological append")
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(row.to_payload(), ensure_ascii=False, allow_nan=False) + "\n")
    assert load(path) == (*prior, row)


@dataclass(frozen=True)
class Prediction:
    ref: str
    scope: str
    context: str
    source_ref: str
    expected: Opening
    created: int
    due: int
    previous_review: Outcome


@dataclass(frozen=True)
class Behavior:
    ref: str
    scope: str
    context: str
    at: int
    summary: str
    opening: Opening | None


@dataclass(frozen=True)
class Feedback:
    ref: str
    behavior_ref: str
    behavior_digest: str
    scope: str
    context: str
    at: int
    desired: Opening | None
    until: int | None
    quote: str
    explicit: bool = True


@dataclass(frozen=True)
class Review:
    outcome: Outcome
    reason: str


def _event_time(experience: Experience) -> int:
    return experience.at


def predict(
    rows: tuple[Experience, ...], *, scope: str, context: str, at: int,
    active_refs: frozenset[str], retrieve: bool = True,
) -> Prediction | None:
    if not retrieve:
        return None
    eligible = tuple(row for row in rows if row.scope == scope and row.context == context
                     and row.ref in active_refs and row.at <= at and (row.until is None or at < row.until))
    if not eligible:
        return None
    source = max(eligible, key=_event_time)
    due = min(at + 5, source.until) if source.until is not None else at + 5
    return Prediction(f"forecast:{source.ref}:{at}", scope, context, source.ref,
                      source.desired, at, due, source.previous_review)


def review(prediction: Prediction | None, behavior: Behavior, feedback: Feedback | None) -> Review:
    if prediction is None or feedback is None or not feedback.explicit or feedback.desired is None:
        return Review("unjudged", "no_attributable_observation")
    if (feedback.behavior_ref != behavior.ref or feedback.behavior_digest != digest(behavior.summary)
            or feedback.scope != behavior.scope or behavior.scope != prediction.scope
            or feedback.context != behavior.context or behavior.context != prediction.context):
        return Review("unjudged", "identity_mismatch")
    if (behavior.opening is None or not feedback.quote
            or not prediction.created <= behavior.at < feedback.at <= prediction.due
            or (feedback.until is not None and feedback.until <= feedback.at)):
        return Review("unjudged", "incomplete_or_outside_horizon")
    return Review("hit" if prediction.expected == feedback.desired else "miss", "explicit_requested_opening")


def update(prediction: Prediction | None, behavior: Behavior, feedback: Feedback | None) -> Experience | None:
    result = review(prediction, behavior, feedback)
    if result.outcome == "unjudged" or feedback is None or feedback.desired is None:
        return None
    return Experience(feedback.ref, feedback.scope, feedback.context, feedback.at, feedback.until,
                      feedback.desired, feedback.quote, behavior.ref, digest(behavior.summary),
                      behavior.summary, result.outcome)


def projection(prediction: Prediction | None, *, active_refs: frozenset[str], at: int, scope: str) -> str:
    if (prediction is None or prediction.scope != scope or prediction.source_ref not in active_refs
            or not prediction.created <= at < prediction.due):
        return ""
    return (
        "World Model interaction forecast (advisory; not a fact, instruction, evidence or permission).\n"
        f"Object: next explicit user feedback on this discussion opening; context={prediction.context}.\n"
        f"Forecast: requested opening likely {prediction.expected.value}; uncalibrated confidence=0.6. "
        f"Basis={prediction.source_ref}; prior forecast review={prediction.previous_review}. "
        f"Synthetic-clock horizon=[{prediction.created},{prediction.due}].\n"
        "No attributable feedback means unjudged. Current explicit instructions override this forecast."
    )


def history_text(rows: tuple[Experience, ...], *, scope: str, at: int, active_refs: frozenset[str]) -> str:
    visible = tuple(row for row in rows if row.scope == scope and row.at <= at and row.ref in active_refs)
    if not visible:
        return ""
    lines = ["Saved personal interactions (historical; context and validity apply):"]
    for row in visible:
        lines.append(f"{row.ref}: context={row.context}; time={row.at}; valid_until={row.until}. "
                     f"Actual reply={row.actual_summary or '(initial manual preference)'}. "
                     f"Later explicit user statement={row.quote}")
    text = "\n".join(lines)
    if len(text) > HISTORY_BUDGET:
        raise ValueError("frozen history budget exceeded; do not silently change semantic inputs")
    return text


def render_sources(history: str, forecast: str, *, scope: str) -> tuple[str, tuple[GhostContextSource, ...]]:
    texts: list[str] = []
    sources: list[GhostContextSource] = []
    for key, value, budget in (("ghost_experiences", history, HISTORY_BUDGET),
                               ("world_model_interaction_review", forecast, FORECAST_BUDGET)):
        def load_value(text: str = value) -> str:
            return text

        source = render_context_source(ContextSource(
            key, load_value, budget, "synthetic_clock_snapshot", "frozen_interaction_trial", scope=scope,
        ))
        if source is not None:
            if source.truncated:
                raise ValueError("frozen source must not truncate")
            texts.append(source.text)
            sources.append(GhostContextSource(key, source.text, budget, scope))
    return "\n\n".join(texts), tuple(sources)


def replies(index: int) -> dict[Opening, str]:
    scenario = index // 2
    first, second = f"cedar{scenario}", f"maple{scenario}"
    choice = first if scenario % 2 == 0 else second
    comparison = f"Option {first} is simpler; option {second} is more flexible."
    recommendation = f"I recommend {choice}."
    return {Opening.COMPARE: f"{comparison} {recommendation}",
            Opening.RECOMMEND: f"{recommendation} {comparison}"}


def context_for(index: int, phase: int) -> str:
    return "incident_status" if phase == 4 and (index // 2) % 2 == 0 else PLAN


def expected(index: int, phase: int) -> Opening:
    if phase in (0, 2):
        return initial(index)
    if phase in (1, 3):
        return opposite(initial(index))
    return Opening.RECOMMEND if context_for(index, phase) != PLAN else initial(index)


def question(index: int, phase: int) -> str:
    options = replies(index)
    override = (f"For this answer, use {initial(index).value}. "
                if phase == 4 and context_for(index, phase) == PLAN else "")
    return (
        f"Current discussion context={context_for(index, phase)}; synthetic clock={10 * (phase + 1)}. "
        "What do you think of this plan? Use the user's applicable discussion-opening preference, "
        "if known; otherwise default to recommendation first. " + override +
        "Do not run tools or modify files. For this probe, call done once with summary exactly one "
        f"of these two full replies (no extra text):\n{options[Opening.COMPARE]}\n{options[Opening.RECOMMEND]}"
    )


def opening_of(summary: str, index: int) -> Opening | None:
    return next((opening for opening, reply in replies(index).items() if summary.strip() == reply), None)


def feedback_for(index: int, phase: int, behavior: Behavior) -> Feedback:
    desired = opposite(initial(index)) if phase in (0, 3) else initial(index)
    until = 35 if phase in (1, 2) else None
    quote = (f"For architecture_plan discussions, I now want {desired.value}. "
             + ("Only until synthetic clock 35." if until else "Use this going forward."))
    return Feedback(f"feedback:{index}:{phase}", behavior.ref, digest(behavior.summary), behavior.scope,
                    behavior.context, behavior.at + 1, desired, until, quote)


def cell(
    home: Path, *, index: int, phase: int, arm: Arm, history: str, forecast: str, scope: str,
) -> tuple[dict[str, object], str]:
    home.mkdir(parents=True, exist_ok=False)
    preparation_started = time.monotonic()
    context, sources = render_sources(history if arm != "none" else "", forecast if arm == "forecast" else "", scope=scope)
    provider = CapturedProvider(home)
    usage: list[ApiExchangeUsage] = []
    provider.bind_usage(f"interaction-{index}-{phase}-{arm}", usage.append)
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
    preparation_ms = (time.monotonic() - preparation_started) * 1000
    started = time.monotonic()
    try:
        result = run_task_kernel(session, request=KernelRunRequestBuilder(
            provider, "r", "trial", "local", task,
            context_text=context, context_scope=scope, ghost_sources=sources,
        ).build(execution=KernelExecutionDeps(intent_sink=sink), observation=KernelObservationDeps(
            propagate_provider_failure=True,
            on_boundary=durable_generation_boundary_callback(durable=durable, session_id="s", task_id="r", sink=sink),
        )))
        elapsed = (time.monotonic() - started) * 1000
        _save(home / "result.json", {"summary": result.summary, "stop_reason": result.stop_reason,
                                     "usage": [row.to_payload() for row in usage]})
        if provider.requests != 1 or len(usage) != 1 or not provider.complete or not result.summary.strip():
            raise ValueError("incomplete generation; never retry")
        reported = usage[0].usage
        if reported.input_tokens is None or reported.output_tokens is None:
            raise ValueError("missing service usage; never retry")
        wire = provider.wire.decode()
        assert ("Saved personal interactions" in wire) == (arm != "none" and bool(history))
        assert ("World Model interaction forecast" in wire) == (arm == "forecast" and bool(forecast))
        return {
            "stream": index, "phase": phase, "arm": arm, "primary": phase < 4,
            "correct": result.summary.strip() == replies(index)[expected(index, phase)],
            "valid_reply": opening_of(result.summary, index) is not None,
            "input_tokens": reported.input_tokens, "output_tokens": reported.output_tokens,
            "requests": provider.requests, "elapsed_ms": elapsed, "visible_ready_ms": elapsed,
            "preparation_ms": preparation_ms,
            "wire_bytes": len(provider.wire), "wire_sha256": digest(wire),
        }, result.summary
    finally:
        provider.close()
        durable.close()


def _integer(row: dict[str, object], key: str) -> int:
    return require_json_integer(row[key], key)


def _float(row: dict[str, object], key: str) -> float:
    value = row[key]
    if not isinstance(value, float):
        raise ValueError("missing numeric metric")
    return value


def summarize(rows: list[dict[str, object]], reviews: list[dict[str, object]], state_sizes: list[int]) -> dict[str, object]:
    identities = {(row["stream"], row["phase"], row["arm"]) for row in rows}
    expected_keys = {(index, phase, arm) for index in range(STREAMS) for phase in range(PHASES) for arm in ARMS}
    if len(rows) != len(expected_keys) or identities != expected_keys:
        raise ValueError("missing or duplicate frozen cells")
    review_keys = {(row["stream"], row["phase"]) for row in reviews}
    if len(reviews) != STREAMS * PHASES or review_keys != {(index, phase) for index in range(STREAMS) for phase in range(PHASES)}:
        raise ValueError("missing or duplicate prediction reviews")
    counts = {arm: sum(row["correct"] is True for row in rows if row["arm"] == arm) for arm in ARMS}
    primary_streams = {arm: sum(all(row["correct"] is True for row in rows
                                  if row["arm"] == arm and row["stream"] == index and row["primary"])
                                for index in range(STREAMS)) for arm in ARMS}
    by_initial = {style.value: sum(all(row["correct"] is True for row in rows
                                     if row["arm"] == "forecast" and row["stream"] == index and row["primary"])
                                   for index in range(STREAMS) if initial(index) == style) for style in Opening}
    by_phase = {str(phase): sum(row["correct"] is True for row in rows
                               if row["arm"] == "forecast" and row["phase"] == phase) for phase in range(PHASES)}
    boundary = {kind: sum(row["correct"] is True for row in rows if row["arm"] == "forecast"
                         and row["phase"] == 4
                         and (context_for(_integer(row, "stream"), 4) != PLAN) == (kind == "context"))
                for kind in ("context", "override")}
    token_deltas: list[int] = []
    for index in range(STREAMS):
        for phase in range(PHASES):
            a = next(row for row in rows if row["stream"] == index and row["phase"] == phase and row["arm"] == "forecast")
            b = next(row for row in rows if row["stream"] == index and row["phase"] == phase and row["arm"] == "retrieval")
            token_deltas.append(_integer(a, "input_tokens") - _integer(b, "input_tokens"))
    p95 = {arm: sorted(_float(row, "elapsed_ms") for row in rows if row["arm"] == arm)[math.ceil(STREAMS * PHASES * .95) - 1]
           for arm in ARMS}
    benefit = (primary_streams["forecast"] >= 18 and primary_streams["forecast"] - primary_streams["retrieval"] >= 4
               and primary_streams["forecast"] - primary_streams["none"] >= 8
               and all(count >= 18 for count in by_phase.values()) and all(count >= 9 for count in boundary.values())
               and all(count >= 9 for count in by_initial.values()))
    cost = (max(token_deltas) <= 400 and p95["forecast"] <= p95["retrieval"] * 2 + 1000
            and len(state_sizes) == STREAMS and max(state_sizes) <= 65536)
    return {
        "status": "EXPERIMENT_COMPLETE", "production_decision": "EXPERIMENT_ONLY",
        "independent_synthetic_streams": STREAMS, "physical_requests": len(rows),
        "correct_cells": counts, "all_four_primary_correct_streams": primary_streams,
        "forecast_phase_correct": by_phase, "forecast_boundary_correct": boundary,
        "forecast_primary_streams_by_initial": by_initial,
        "max_extra_input_tokens_vs_same_history": max(token_deltas), "p95_ms": p95,
        "max_state_bytes": max(state_sizes), "benefit_gate": benefit, "cost_gate": cost,
        "eligible_for_production_boundaries": benefit and cost,
        "reviews": reviews, "cells": rows,
    }


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
            path = stream_home / "experiences.jsonl"
            scope = request_scope(stream_home, f"user-{index}")
            seed = Experience(f"manual:{index}", scope, PLAN, 0, None, initial(index),
                              f"For architecture_plan discussions, I want {initial(index).value}. Use this going forward.")
            append(path, seed)
            for phase in range(PHASES):
                experiences = load(path)
                active = frozenset(row.ref for row in experiences)
                at = 10 * (phase + 1)
                prediction = predict(experiences, scope=scope, context=context_for(index, phase), at=at, active_refs=active)
                forecast = projection(prediction, active_refs=active, at=at, scope=scope)
                history = history_text(experiences, scope=scope, at=at, active_refs=active)
                order: tuple[Arm, ...] = ("none", "retrieval", "forecast")
                rotation = (index + phase) % len(order)
                candidate_summary = ""
                for arm in (*order[rotation:], *order[:rotation]):
                    metric, summary = cell(stream_home / f"phase-{phase}" / arm, index=index, phase=phase, arm=arm,
                                           history=history, forecast=forecast, scope=scope)
                    rows.append(metric)
                    if arm == "forecast":
                        candidate_summary = summary
                    _save(home / "completed.json", rows)
                    print(f"stream={index} phase={phase} arm={arm} correct={metric['correct']}", flush=True)
                behavior = Behavior(f"behavior:{index}:{phase}", scope, context_for(index, phase), at,
                                    candidate_summary, opening_of(candidate_summary, index))
                feedback = feedback_for(index, phase, behavior) if phase < 4 else None
                result = review(prediction, behavior, feedback)
                reviews.append({"stream": index, "phase": phase, "outcome": result.outcome, "reason": result.reason})
                _save(stream_home / f"prediction-review-{phase}.json", {
                    "prediction": {"ref": prediction.ref, "source_ref": prediction.source_ref,
                                   "scope": prediction.scope, "context": prediction.context,
                                   "expected": prediction.expected.value, "created": prediction.created,
                                   "due": prediction.due, "previous_review": prediction.previous_review} if prediction else None,
                    "behavior": {"ref": behavior.ref, "scope": scope, "context": behavior.context,
                                 "at": at, "summary": candidate_summary,
                                 "opening": behavior.opening.value if behavior.opening else None},
                    "feedback": {"ref": feedback.ref, "behavior_ref": feedback.behavior_ref,
                                 "behavior_digest": feedback.behavior_digest, "scope": feedback.scope,
                                 "context": feedback.context, "at": feedback.at, "until": feedback.until,
                                 "desired": feedback.desired.value if feedback.desired else None,
                                 "quote": feedback.quote, "explicit": feedback.explicit} if feedback else None,
                    "review": {"outcome": result.outcome, "reason": result.reason},
                })
                revised = update(prediction, behavior, feedback)
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
        raise ValueError("explicit --live is required")
    run(options.output)


if __name__ == "__main__":
    main()
