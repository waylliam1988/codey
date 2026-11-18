"""Frozen conditional acceptance/correction-cost consumer; experiment only."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
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
from tests.manual.ghost_memory_routing.project_experience_continuity import _models, _save
from tests.manual.ghost_memory_routing.semantic_goal_trial import CapturedProvider

STREAMS = 20
PHASES = 3
Arm = Literal["none", "retrieval", "counts", "forecast"]
ARMS: tuple[Arm, ...] = ("none", "retrieval", "counts", "forecast")
Outcome = Literal["hit", "miss", "unjudged"]
HISTORY_BUDGET = 24000
COUNTS_BUDGET = 1400
FORECAST_BUDGET = 1600
STATE_LIMIT = 131072
ASK = "ASK"


class Context(StrEnum):
    FIELD = "field_storage"
    TEAM = "team_storage"


class Strategy(StrEnum):
    LOCAL = "local_journal"
    SHARED = "shared_service"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def preferred(index: int, context: Context) -> Strategy:
    return Strategy.LOCAL if (index % 2 == 0) == (context == Context.FIELD) else Strategy.SHARED


def context_for(phase: int) -> Context:
    return Context.TEAM if phase == 2 else Context.FIELD


def assessment_quote(accepted: bool, strategy: Strategy, context: Context) -> str:
    return f"I explicitly {'accept' if accepted else 'reject'} this {strategy.value} proposal for {context.value}."


@dataclass(frozen=True)
class Event:
    ref: str
    scope: str
    context: Context
    strategy: Strategy
    at: int
    accepted: bool
    quote: str
    reply_ref: str
    reply_digest: str
    reply_at: int
    actual_summary: str

    def payload(self) -> dict[str, object]:
        return {"schema": 1, "ref": self.ref, "scope": self.scope, "context": self.context.value,
                "strategy": self.strategy.value, "at": self.at, "accepted": self.accepted, "quote": self.quote,
                "reply_ref": self.reply_ref, "reply_digest": self.reply_digest, "reply_at": self.reply_at,
                "actual_summary": self.actual_summary}

    @classmethod
    def decode(cls, value: dict[str, object]) -> Event:
        if set(value) != {"schema", "ref", "scope", "context", "strategy", "at", "accepted", "quote",
                          "reply_ref", "reply_digest", "reply_at", "actual_summary"}:
            raise ValueError("invalid event fields")
        if type(value["schema"]) is not int or value["schema"] != 1 or type(value["accepted"]) is not bool:
            raise ValueError("invalid event schema or acceptance")
        accepted = value["accepted"]
        assert isinstance(accepted, bool)
        row = cls(require_json_string(value["ref"], "ref"), require_json_string(value["scope"], "scope"),
                  Context(require_json_string(value["context"], "context")),
                  Strategy(require_json_string(value["strategy"], "strategy")),
                  require_json_integer(value["at"], "at"), accepted,
                  require_json_string(value["quote"], "quote"), require_json_string(value["reply_ref"], "reply_ref"),
                  require_json_string(value["reply_digest"], "reply_digest"), require_json_integer(value["reply_at"], "reply_at"),
                  require_json_string(value["actual_summary"], "actual_summary"))
        if (not row.ref or not row.scope or row.quote != assessment_quote(row.accepted, row.strategy, row.context)
                or not row.reply_ref or not row.actual_summary.strip()
                or not 0 <= row.reply_at < row.at or row.reply_digest != digest(row.actual_summary)):
            raise ValueError("invalid event identity, chronology or actual reply binding")
        return row


def load(path: Path) -> tuple[Event, ...]:
    if not path.exists():
        return ()
    rows = tuple(Event.decode(decoded_object(line)) for line in path.read_text(encoding="utf-8").splitlines())
    if len({row.ref for row in rows}) != len(rows) or any(a.at >= b.at for a, b in zip(rows, rows[1:], strict=False)):
        raise ValueError("duplicate or nonchronological events")
    return rows


def append(path: Path, row: Event) -> None:
    validated = Event.decode(row.payload())
    prior = load(path)
    if any(old.ref == row.ref for old in prior) or (prior and prior[-1].at >= row.at):
        raise ValueError("duplicate or nonchronological append")
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(validated.payload(), ensure_ascii=False, allow_nan=False) + "\n")
    assert load(path) == (*prior, row)


def seed_events(index: int, scope: str) -> tuple[Event, ...]:
    scenario = index // 2
    n = 17 + scenario % 5
    high = n - (4 + scenario % 3)
    rows: list[Event] = []
    for context in Context:
        for strategy in Strategy:
            accepted_count = high if strategy == preferred(index, context) else n - high
            # A fixed permutation prevents a chronological all-positive prefix.
            def permutation(position: int, saved_context: Context = context, saved_strategy: Strategy = strategy) -> str:
                return digest(f"seed-order:{scenario}:{saved_context}:{saved_strategy}:{position}")

            positives = frozenset(sorted(range(n), key=permutation)[:accepted_count])
            for position in range(n):
                number = len(rows)
                summary = f"Historical synthetic reply proposing {strategy.value}."
                accepted = position in positives
                rows.append(Event(f"seed:{number}", scope, context, strategy, number * 2 + 1, accepted,
                                  assessment_quote(accepted, strategy, context),
                                  f"seed-reply:{number}", digest(summary), number * 2, summary))
    return tuple(rows)


def eligible(rows: tuple[Event, ...], *, scope: str, at: int, active: frozenset[str],
             retrieve: bool = True) -> tuple[Event, ...]:
    return tuple(row for row in rows if retrieve and row.scope == scope and row.at <= at and row.ref in active)


def history_text(rows: tuple[Event, ...]) -> str:
    text = "\n".join(["Saved explicit proposal assessments (historical observations, not fixed user instructions):",
                      *(f"{row.ref}; time={row.at}; actual reply={row.actual_summary}; user={row.quote}" for row in rows)]) if rows else ""
    if len(text) > HISTORY_BUDGET:
        raise ValueError("frozen history budget exceeded")
    return text


def count(rows: tuple[Event, ...], context: Context, strategy: Strategy) -> tuple[int, int]:
    matching = tuple(row for row in rows if row.context == context and row.strategy == strategy)
    return sum(row.accepted for row in matching), len(matching)


def counts_text(rows: tuple[Event, ...]) -> str:
    if not rows:
        return ""
    return "\n".join(["Simple conditional-count baseline (same explicit observations; no new feedback):",
                      *(f"context={context.value}; strategy={strategy.value}; accepted={count(rows, context, strategy)[0]}; "
                        f"total={count(rows, context, strategy)[1]}." for context in Context for strategy in Strategy)])


@dataclass(frozen=True)
class Option:
    label: str
    strategy: Strategy
    benefit: int
    correction_cost: int


def options(index: int, phase: int) -> tuple[Option, Option]:
    scenario = index // 2
    mode = scenario % 3
    local_cost = 27 + scenario % 5 + phase * 2 if mode == 2 else 15 + phase
    shared_cost = 27 + scenario % 5 + phase * 2 if mode == 1 else 15 + phase
    return (Option(f"cedar{scenario}p{phase}", Strategy.LOCAL, 9 + phase, local_cost),
            Option(f"maple{scenario}p{phase}", Strategy.SHARED, 9 + phase, shared_cost))


def ask_cost(index: int) -> int:
    return 1 + (index // 2) % 2


def probability(rows: tuple[Event, ...], context: Context, strategy: Strategy) -> Fraction:
    accepted, total = count(rows, context, strategy)
    return Fraction(accepted + 1, total + 2)


def utility(p: Fraction, option: Option) -> Fraction:
    return p * option.benefit - (1 - p) * option.correction_cost


def expected(rows: tuple[Event, ...], index: int, phase: int) -> str:
    scored: list[tuple[Fraction, str]] = [(Fraction(-ask_cost(index)), ASK)]
    for option in options(index, phase):
        scored.append((utility(probability(rows, context_for(phase), option.strategy), option), option.label))
    highest = max(score for score, _label in scored)
    return next(label for score, label in scored if score == highest)


def question(index: int, phase: int) -> str:
    return (f"Review new storage proposals in context={context_for(phase).value}. "
            "Using only this user's matching explicit past assessments, estimate acceptance with Laplace smoothing "
            "p=(accepted+1)/(total+2). Expected utility is p*benefit-(1-p)*correction_cost. "
            f"Asking for clarification (ASK) has certain utility -{ask_cost(index)}. "
            + " ".join(f"{option.label}: strategy={option.strategy.value}, benefit={option.benefit}, "
                       f"correction_cost={option.correction_cost}." for option in options(index, phase))
            + " Choose the highest expected utility, not the most recently accepted strategy. "
            "On an exact tie prefer ASK, then the first listed proposal. If no relevant personal history exists, choose ASK. "
            "This is advice only: do not run tools or modify files. Call done once with summary exactly ASK or "
            + " or ".join(option.label for option in options(index, phase)) + ". No extra text.")


@dataclass(frozen=True)
class Prediction:
    ref: str
    scope: str
    context: Context
    strategy: Strategy
    option: str
    basis: tuple[str, ...]
    numerator: int
    denominator: int
    created: int
    due: int

    @property
    def p(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    def payload(self) -> dict[str, object]:
        return {"ref": self.ref, "scope": self.scope, "context": self.context.value,
                "strategy": self.strategy.value, "option": self.option, "basis": self.basis,
                "numerator": self.numerator, "denominator": self.denominator,
                "created": self.created, "due": self.due}


def valid_prediction(row: Prediction) -> bool:
    return (bool(row.ref and row.scope and row.option and row.basis)
            and len(set(row.basis)) == len(row.basis) and all(row.basis)
            and type(row.numerator) is int and type(row.denominator) is int
            and 0 < row.numerator < row.denominator
            and type(row.created) is int and type(row.due) is int and 0 <= row.created < row.due)


def predict(rows: tuple[Event, ...], *, index: int, phase: int, scope: str, at: int) -> tuple[Prediction, ...]:
    predictions: list[Prediction] = []
    for option in options(index, phase):
        matching = tuple(row for row in rows if row.scope == scope and row.context == context_for(phase)
                         and row.strategy == option.strategy and row.at <= at)
        if not matching:
            continue
        p = probability(matching, context_for(phase), option.strategy)
        predictions.append(Prediction(f"forecast:{index}:{phase}:{option.label}", scope, context_for(phase),
                                      option.strategy, option.label, tuple(row.ref for row in matching),
                                      p.numerator, p.denominator, at, at + 5))
    return tuple(predictions)


def projection(predictions: tuple[Prediction, ...], *, index: int, phase: int, scope: str,
               at: int, active: frozenset[str]) -> str:
    menu = options(index, phase)
    if len(predictions) != 2 or any(not valid_prediction(row) or row.scope != scope or row.context != context_for(phase)
                                   or not row.created <= at < row.due or not set(row.basis).issubset(active)
                                   for row in predictions):
        return ""
    if any(row.option != option.label or row.strategy != option.strategy
           for row, option in zip(predictions, menu, strict=True)):
        return ""
    lines = ["World Model conditional acceptance forecast (uncertain hypothesis; never a user fact, instruction, evidence or permission).",
             f"Object: next complete explicit user assessment of both new options; context={context_for(phase).value}."]
    for prediction, option in zip(predictions, menu, strict=True):
        lines.append(f"{prediction.option}: predicted acceptance={prediction.numerator}/{prediction.denominator} "
                     f"({float(prediction.p):.6f}); predicted expected utility={float(utility(prediction.p, option)):.6f}; "
                     f"basis_count={len(prediction.basis)}; basis_digest={digest('|'.join(prediction.basis))}; "
                     f"horizon=[{prediction.created},{prediction.due}].")
    lines.append(f"Clarification utility=-{ask_cost(index)}. Probabilities are empirical Laplace estimates, not calibrated guarantees. "
                 "Missing or unattributable feedback is unjudged. Current explicit instructions take precedence.")
    text = "\n".join(lines)
    if len(text) > FORECAST_BUDGET:
        raise ValueError("frozen forecast budget exceeded")
    return text


@dataclass(frozen=True)
class Behavior:
    ref: str
    scope: str
    context: Context
    at: int
    summary: str


def heldout_acceptance(index: int, phase: int, strategy: Strategy) -> bool:
    threshold = 78 if strategy == preferred(index, context_for(phase)) else 22
    # Independent sealed draws, never generated from a model response or posterior estimate.
    draw = int(digest(f"sealed-user:{index}:{phase}:{strategy.value}")[:8], 16) % 100
    return draw < threshold


def feedback(index: int, phase: int, behavior: Behavior) -> tuple[Event, ...]:
    if phase == PHASES - 1:
        return ()
    return tuple(Event(f"feedback:{index}:{phase}:{strategy.value}", behavior.scope, behavior.context, strategy,
                       behavior.at + position + 1, heldout_acceptance(index, phase, strategy),
                       assessment_quote(heldout_acceptance(index, phase, strategy), strategy, behavior.context), behavior.ref,
                       digest(behavior.summary), behavior.at, behavior.summary)
                 for position, strategy in enumerate(Strategy))


def review(prediction: Prediction, behavior: Behavior, event: Event | None, *, active: frozenset[str]) -> tuple[Outcome, float | None]:
    if event is None or not valid_prediction(prediction):
        return "unjudged", None
    try:
        Event.decode(event.payload())
    except ValueError:
        return "unjudged", None
    if (not behavior.summary.strip() or event.reply_ref != behavior.ref
            or event.reply_digest != digest(behavior.summary) or event.actual_summary != behavior.summary
            or event.scope != behavior.scope or behavior.scope != prediction.scope
            or event.context != behavior.context or behavior.context != prediction.context
            or event.strategy != prediction.strategy or event.reply_at != behavior.at
            or not prediction.created <= behavior.at < event.at <= prediction.due
            or not set(prediction.basis).issubset(active) or not event.quote):
        return "unjudged", None
    observed = int(event.accepted)
    error = float((prediction.p - observed) ** 2)
    return ("hit" if (prediction.p >= Fraction(1, 2)) == event.accepted else "miss"), error


def render_sources(history: str, counts: str, forecast: str, *, scope: str) -> tuple[str, tuple[GhostContextSource, ...]]:
    text: list[str] = []
    sources: list[GhostContextSource] = []
    for key, value, budget in (("ghost_experiences", history, HISTORY_BUDGET),
                               ("ghost_proposal_counts", counts, COUNTS_BUDGET),
                               ("world_model_conditional_acceptance", forecast, FORECAST_BUDGET)):
        def load_value(content: str = value) -> str:
            return content

        rendered = render_context_source(ContextSource(key, load_value, budget, "synthetic_clock_snapshot",
                                                       "frozen_proposal_risk_trial", scope=scope))
        if rendered is not None:
            if rendered.truncated:
                raise ValueError("frozen source must not truncate")
            text.append(rendered.text)
            sources.append(GhostContextSource(key, rendered.text, budget, scope))
    return "\n\n".join(text), tuple(sources)


def cell(home: Path, *, index: int, phase: int, arm: Arm, rows: tuple[Event, ...],
         forecast: str, scope: str) -> tuple[dict[str, object], str]:
    home.mkdir(parents=True, exist_ok=False)
    preparation_start = time.monotonic()
    context, sources = render_sources(history_text(rows) if arm != "none" else "",
                                      counts_text(rows) if arm in {"counts", "forecast"} else "",
                                      forecast if arm == "forecast" else "", scope=scope)
    provider = CapturedProvider(home)
    usage: list[ApiExchangeUsage] = []
    provider.bind_usage(f"risk-{index}-{phase}-{arm}", usage.append)
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
        assert ("Saved explicit proposal assessments" in wire) == (arm != "none")
        assert ("Simple conditional-count baseline" in wire) == (arm in {"counts", "forecast"})
        assert ("World Model conditional acceptance forecast" in wire) == (arm == "forecast")
        return {"stream": index, "phase": phase, "arm": arm,
                "correct": result.summary.strip() == expected(rows, index, phase),
                "valid_reply": result.summary.strip() in {ASK, *(option.label for option in options(index, phase))},
                "expected_action": expected(rows, index, phase), "input_tokens": reported.input_tokens,
                "output_tokens": reported.output_tokens, "requests": provider.requests,
                "elapsed_ms": elapsed, "visible_ready_ms": elapsed, "preparation_ms": preparation_ms,
                "wire_bytes": len(provider.wire), "wire_sha256": digest(wire)}, result.summary
    finally:
        provider.close()
        durable.close()


def latency(row: dict[str, object]) -> float:
    value = row["elapsed_ms"]
    if not isinstance(value, float):
        raise ValueError("invalid latency")
    return value


def summarize(rows: list[dict[str, object]], reviews: list[dict[str, object]], sizes: list[int]) -> dict[str, object]:
    keys = {(i, phase, arm) for i in range(STREAMS) for phase in range(PHASES) for arm in ARMS}
    if len(rows) != len(keys) or {(row["stream"], row["phase"], row["arm"]) for row in rows} != keys:
        raise ValueError("missing or duplicate frozen cells")
    review_keys = {(i, phase, strategy.value) for i in range(STREAMS) for phase in range(PHASES) for strategy in Strategy}
    if len(reviews) != len(review_keys) or {(row["stream"], row["phase"], row["strategy"]) for row in reviews} != review_keys:
        raise ValueError("missing or duplicate reviews")
    complete = {arm: sum(all(row["correct"] is True for row in rows if row["arm"] == arm and row["stream"] == i)
                         for i in range(STREAMS)) for arm in ARMS}
    phases = {arm: [sum(row["correct"] is True for row in rows if row["arm"] == arm and row["phase"] == phase)
                    for phase in range(PHASES)] for arm in ARMS}
    groups = {str(parity): sum(all(row["correct"] is True for row in rows if row["arm"] == "forecast" and row["stream"] == i)
                              for i in range(parity, STREAMS, 2)) for parity in range(2)}
    p95 = {arm: sorted(latency(row) for row in rows if row["arm"] == arm)[math.ceil(STREAMS * PHASES * .95) - 1] for arm in ARMS}
    tokens = {(require_json_integer(row["stream"], "stream"), require_json_integer(row["phase"], "phase"), row["arm"]):
              require_json_integer(row["input_tokens"], "input_tokens") for row in rows}
    delta = max(tokens[i, phase, "forecast"] - tokens[i, phase, "counts"] for i in range(STREAMS) for phase in range(PHASES))
    briers: list[float] = []
    for row in reviews:
        brier = row["brier"]
        if brier is not None:
            if not isinstance(brier, float) or not 0 <= brier <= 1:
                raise ValueError("invalid review error")
            briers.append(brier)
    prediction_gate = len(briers) == STREAMS * 4 and sum(briers) / len(briers) <= .25
    benefit = (complete["forecast"] >= 18 and complete["forecast"] - complete["counts"] >= 4
               and complete["forecast"] - complete["retrieval"] >= 4 and complete["forecast"] - complete["none"] >= 8
               and min(phases["forecast"]) >= 18 and min(groups.values()) >= 9 and prediction_gate)
    cost = len(sizes) == STREAMS and max(sizes) <= STATE_LIMIT and delta <= 600 and p95["forecast"] <= p95["counts"] * 2 + 1000
    return {"status": "EXPERIMENT_COMPLETE", "production_decision": "EXPERIMENT_ONLY", "independent_synthetic_streams": STREAMS,
            "physical_requests": len(rows), "all_three_correct_streams": complete, "phase_correct": phases,
            "forecast_streams_by_initial": groups, "p95_ms": p95, "max_extra_input_tokens": delta,
            "correct_cells": {arm: sum(row["correct"] is True for row in rows if row["arm"] == arm) for arm in ARMS},
            "valid_cells": {arm: sum(row["valid_reply"] is True for row in rows if row["arm"] == arm) for arm in ARMS},
            "brier_count": len(briers), "mean_brier": sum(briers) / len(briers) if briers else None,
            "prediction_quality_gate": prediction_gate, "benefit_gate": benefit, "cost_gate": cost,
            "max_derived_state_bytes": max(sizes), "eligible_for_production_boundaries": benefit and cost,
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
            path = stream_home / "assessments.jsonl"
            scope = request_scope(stream_home, f"user-{index}")
            for row in seed_events(index, scope):
                append(path, row)
            for phase in range(PHASES):
                started = time.monotonic()
                saved = load(path)
                active = frozenset(row.ref for row in saved)
                at = 1000 * (phase + 1)
                matching = eligible(saved, scope=scope, at=at, active=active)
                predictions = predict(matching, index=index, phase=phase, scope=scope, at=at)
                hint = projection(predictions, index=index, phase=phase, scope=scope, at=at, active=active)
                state_ms = (time.monotonic() - started) * 1000
                _save(stream_home / f"before-{phase}.json", {"at": at, "basis": sorted(active),
                      "predictions": [prediction.payload() for prediction in predictions], "projection": hint,
                      "expected_action": expected(matching, index, phase)})
                rotation = (index + phase) % len(ARMS)
                summary = ""
                for arm in (*ARMS[rotation:], *ARMS[:rotation]):
                    metric, actual = cell(stream_home / f"phase-{phase}" / arm, index=index, phase=phase,
                                          arm=arm, rows=matching, forecast=hint, scope=scope)
                    metric["shared_state_preparation_ms"] = state_ms
                    rows.append(metric)
                    if arm == "forecast":
                        summary = actual
                    _save(home / "completed.json", rows)
                    print(f"stream={index} phase={phase} arm={arm} correct={metric['correct']}", flush=True)
                behavior = Behavior(f"actual-reply:{index}:{phase}", scope, context_for(phase), at, summary)
                observations = feedback(index, phase, behavior)
                reviewed: list[dict[str, object]] = []
                for prediction in predictions:
                    event = next((row for row in observations if row.strategy == prediction.strategy), None)
                    outcome, brier = review(prediction, behavior, event, active=active)
                    entry: dict[str, object] = {"stream": index, "phase": phase, "strategy": prediction.strategy.value,
                                                "outcome": outcome, "brier": brier}
                    reviews.append(entry)
                    reviewed.append(entry)
                _save(stream_home / f"review-{phase}.json", {"predictions": [p.payload() for p in predictions],
                      "actual_reply": {"ref": behavior.ref, "scope": scope, "context": behavior.context.value,
                                       "at": at, "summary": summary},
                      "feedback": [event.payload() for event in observations], "reviews": reviewed})
                for event in observations:
                    append(path, event)
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
