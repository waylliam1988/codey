"""Isolated World Model experiment; never imported by production.

Verification produces the proof. The predictor only reads committed, bounded
events and returns advisory context; no prediction can write a proof or fact.
Run from the repository root with ``python -m tests.manual.ghost_memory_routing.world_model_verification_review``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal

from codey.completion.contract import (
    CHECK_FAIL,
    CHECK_PASS,
    CompletionProof,
    build_completion_contract,
    completion_check,
    project_completion_proof,
)
from codey.completion.verification import classify_verification_failure
from codey.ghost.graph_primitives import parse_ts
from codey.ghost.payload_types import object_fields
from codey.providers.api_provider import ApiProvider
from codey.providers.api_request_policy import LocalRequestPolicy
from codey.providers.base import TurnFinish
from codey.providers.token_accounting import ApiExchangeUsage, ReportedUsage
from codey.runtime.observe.prompt_envelope import PromptEnvelope, PromptEnvelopeSection
from codey.workspace.context_admission import GhostContextSource, admit_ghost_context
from codey.workspace.context_source import ContextSource, render_context_source

MODEL = "koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M"
ENDPOINT = "http://127.0.0.1:5001/v1"
AS_OF = "2026-11-18T00:00:00Z"
SECOND = "2026-11-18T00:01:00Z"
THIRD = "2026-11-18T00:02:00Z"
EXPIRY = "2026-11-19T00:00:00Z"
BUDGET = 900
Category = Literal["verified", "environment_failure", "product_failure"]
ReviewOutcome = Literal["hit", "miss", "unjudged"]
Action = Literal["inspect_environment", "report_verified", "inspect_source", "verify"]
KINDS: tuple[Category, ...] = ("verified", "environment_failure", "product_failure")
ACTIONS: tuple[Action, ...] = ("inspect_environment", "report_verified", "inspect_source", "verify")
QUESTIONS = (
    "Choose the next local step from the current verification facts.",
    "What should the operator do next? Use the latest observed result.",
    "Review whether to inspect environment, inspect source, verify, or report verified.",
    "Give the next action for this completed coding attempt without running anything.",
)


def digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class VerificationEvent:
    source_ref: str
    command_digest: str
    workspace_fingerprint: str
    scope: str
    occurred_at: str
    category: Category
    proof_status: str

    def to_payload(self) -> dict[str, str]:
        return {
            "source_ref": self.source_ref, "command_digest": self.command_digest,
            "workspace_fingerprint": self.workspace_fingerprint, "scope": self.scope,
            "occurred_at": self.occurred_at, "category": self.category, "proof_status": self.proof_status,
        }

    @property
    def payload_digest(self) -> str:
        return digest(self.to_payload())


@dataclass(frozen=True)
class Prediction:
    prediction_id: str
    source_ref: str
    command_digest: str
    workspace_fingerprint: str
    scope: str
    expected: Category
    confidence: float
    created_at: str
    due_at: str


@dataclass(frozen=True)
class Review:
    outcome: ReviewOutcome
    reason_code: str
    review_ref: str = ""
    observed: Category | None = None


def predict(event: VerificationEvent) -> Prediction:
    # The candidate under test is intentionally simple persistence prediction.
    # Its confidence is an uncalibrated prediction value, never truth confidence.
    due = (parse_ts(event.occurred_at) + timedelta(days=1)).isoformat()
    return Prediction(
        "prediction:" + event.payload_digest.removeprefix("sha256:"), event.source_ref,
        event.command_digest, event.workspace_fingerprint, event.scope, event.category,
        0.6, event.occurred_at, due,
    )


def review(prediction: Prediction, event: VerificationEvent | None, *, as_of: str) -> Review:
    moment = parse_ts(as_of)
    if event is None:
        return Review("unjudged", "verification_missing")
    if (event.scope != prediction.scope or event.command_digest != prediction.command_digest
            or event.workspace_fingerprint != prediction.workspace_fingerprint):
        return Review("unjudged", "identity_mismatch")
    observed_at = parse_ts(event.occurred_at)
    if not (parse_ts(prediction.created_at) < observed_at <= moment < parse_ts(prediction.due_at)):
        return Review("unjudged", "verification_not_due_or_expired")
    return Review(
        "hit" if event.category == prediction.expected else "miss", "verification_observed",
        event.source_ref, event.category,
    )


def projection(
    prediction: Prediction, result: Review, *, scope: str, as_of: str,
    active_refs: frozenset[str], retrieve: bool = True,
) -> str:
    if (not retrieve or scope != prediction.scope or prediction.source_ref not in active_refs
            or parse_ts(as_of) >= parse_ts(prediction.due_at)):
        return ""
    if result.outcome == "unjudged" or not result.review_ref or result.review_ref not in active_refs:
        return ""
    return (
        "World Model prediction review (advisory, never evidence or completion proof).\n"
        f"Prior forecast: next same-command result would be {prediction.expected}; "
        f"prediction confidence={prediction.confidence}.\n"
        f"Review: {result.outcome}; later verification category={result.observed}.\n"
        "This is a reviewed historical forecast, not a fact about a changed workspace. "
        "Use the current verification proof; never skip verification based on this forecast."
    )


def render_request(
    base: str, hint: str, *, scope: str, as_of: str, admit: bool = True,
    audit: list[dict[str, object]] | None = None,
) -> str:
    source = render_context_source(ContextSource(
        key="world_model_verification_review", loader=lambda: hint, budget=BUDGET,
        freshness="verification_review", why_included="experimental_prediction_review",
        scope=scope,
    ))
    sections = [PromptEnvelopeSection("current_verification", base)]
    candidates: tuple[GhostContextSource, ...] = ()
    if source is not None:
        if source.truncated:
            raise ValueError("World Model projection was truncated")
        sections.append(PromptEnvelopeSection(
            source.key, source.text, source_refs=("context_source:" + source.key,), budget=BUDGET,
        ))
        candidates = (GhostContextSource(source.key, source.text, BUDGET, scope, expires_at=EXPIRY),)
    prompt = PromptEnvelope(sections).render().text
    admission = admit_ghost_context(
        prompt, candidates, request_scope=scope, require_all=True, admit_hints=admit, as_of=as_of,
    )
    if len(admission.accepted) != len(candidates):
        raise ValueError("World Model source was not admitted")
    if audit is not None:
        audit.append(admission.epoch.to_payload())
    return prompt


def produce_verification(
    home: Path, *, mode: str, scope: str, observed_at: str, fingerprint: str,
) -> tuple[VerificationEvent, CompletionProof]:
    """Fixture producer: execute a local check, then let completion own proof.

    The predictor never calls this producer. Each result is committed before
    it is offered as an event; the file is a local experiment fixture only.
    """
    home.mkdir(parents=True, exist_ok=True)
    script = home / "check.py"
    if mode == "environment":
        script.write_text("import codey_world_model_missing_fixture_dependency\n", encoding="utf-8")
    elif mode == "product":
        script.write_text("assert 1 == 2, 'fixture product invariant'\n", encoding="utf-8")
    elif mode == "pass":
        script.write_text("assert 1 + 1 == 2\n", encoding="utf-8")
    else:
        raise ValueError("unknown verification fixture")
    completed = subprocess.run(
        [sys.executable, "check.py"], cwd=home, text=True, capture_output=True, timeout=10, check=False,
    )
    status = CHECK_PASS if completed.returncode == 0 else CHECK_FAIL
    contract = build_completion_contract(
        domain="coding", subject_ref="run:" + digest((mode, scope, observed_at)).removeprefix("sha256:"),
        checks=(completion_check("relevant_verification", status),),
        external_refs=("workspace_fingerprint:" + fingerprint,),
    )
    proof = project_completion_proof(contract)
    if proof is None:
        raise ValueError("verification fixture produced no proof")
    failure_class = classify_verification_failure(
        proof_status=proof.status, decisive_exit_code=completed.returncode,
        decisive_result_summary=completed.stderr,
    )
    category: Category
    if proof.satisfied:
        category = "verified"
    elif failure_class == "environment_failure":
        category = "environment_failure"
    elif failure_class == "product_failure":
        category = "product_failure"
    else:
        raise ValueError("unclassifiable verification fixture")
    event = VerificationEvent(
        proof.proof_id, digest("python check.py"), fingerprint, scope, observed_at, category, proof.status,
    )
    payload: dict[str, object] = {"event": event.to_payload(), "event_digest": event.payload_digest,
                                  "proof": proof.to_payload()}
    (home / "verification.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return event, proof


def load_event(path: Path) -> VerificationEvent | None:
    """Validate the experiment producer's saved bounded event and proof binding."""
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
        if not object_fields(raw):
            return None
        event = raw.get("event")
        proof = raw.get("proof")
        if not object_fields(event) or not object_fields(proof):
            return None
        fields = tuple(event.get(key) for key in (
            "source_ref", "command_digest", "workspace_fingerprint", "scope", "occurred_at", "proof_status",
        ))
        if not all(isinstance(value, str) and value for value in fields):
            return None
        ref, command, fingerprint, scope, timestamp, status = fields
        # Explicit guards establish concrete types for all three checkers.
        if not (isinstance(ref, str) and isinstance(command, str) and isinstance(fingerprint, str)
                and isinstance(scope, str) and isinstance(timestamp, str) and isinstance(status, str)):
            return None
        category = event.get("category")
        if category == "verified":
            kind: Category = "verified"
        elif category == "environment_failure":
            kind = "environment_failure"
        elif category == "product_failure":
            kind = "product_failure"
        else:
            return None
        parsed = VerificationEvent(ref, command, fingerprint, scope, timestamp, kind, status)
        parse_ts(timestamp)
        if (parsed.payload_digest != raw.get("event_digest") or ref != proof.get("proof_id")
                or status != proof.get("status") or not ref.startswith("completion_proof:")):
            return None
        if (kind == "verified") != (status == "complete" and proof.get("satisfied") is True):
            return None
        return parsed
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class Case:
    name: str
    prior: str
    current: str | None
    changed_workspace: bool
    expected: Action


CASES = (
    Case("repeated_environment", "environment", "environment", False, "inspect_environment"),
    Case("recovered_environment", "environment", "pass", False, "report_verified"),
    Case("product_failure", "product", "product", False, "inspect_source"),
    Case("missing_verification", "environment", None, False, "verify"),
    Case("changed_workspace", "pass", None, True, "verify"),
)


def fixture(home: Path, case: Case) -> tuple[str, str, Review]:
    prior, _ = produce_verification(home / "prior", mode=case.prior, scope="user",
                                    observed_at=AS_OF, fingerprint="fixture-workspace-v1")
    # Consumers read the committed boundary rather than producer objects.
    restored = load_event(home / "prior" / "verification.json")
    if restored is None or restored != prior:
        raise ValueError("committed prior event does not round-trip")
    current: VerificationEvent | None = None
    if case.current is not None:
        produced, _ = produce_verification(home / "current", mode=case.current, scope="user",
                                          observed_at=SECOND, fingerprint="fixture-workspace-v1")
        current = load_event(home / "current" / "verification.json")
        if current != produced:
            raise ValueError("committed current event does not round-trip")
    prediction = predict(restored)
    result = review(prediction, current, as_of=THIRD)
    refs: set[str] = {prior.source_ref}
    if current is not None:
        refs.add(current.source_ref)
    hint = projection(prediction, result, scope="user", as_of=THIRD, active_refs=frozenset(refs))
    current_facts = (f"Current verification category={current.category}; proof status={current.proof_status}."
                     if current else "Current workspace has no observed verification or satisfied proof.")
    baseline = (
        "You are reviewing a coding attempt. Do not run tools. "
        "Return exactly one JSON object with one key action, whose value is one of "
        "inspect_environment, report_verified, inspect_source, verify.\n"
        "Runtime rules: environment failure is not source edit proof. "
        "Only current local verification can support reporting verified; missing or changed-workspace "
        "verification must be rechecked.\n"
        f"Prior verification category={prior.category}; proof status={prior.proof_status}.\n"
        f"Workspace changed since prior observation={case.changed_workspace}.\n{current_facts}"
    )
    return baseline, hint, result


def action(reply: str) -> str:
    try:
        value: object = json.loads(reply)
    except ValueError:
        return "invalid_json"
    if not object_fields(value) or set(value) != {"action"}:
        return "invalid_structure"
    selected = value.get("action")
    return selected if isinstance(selected, str) and selected in ACTIONS else "invalid_action"


def parse_usage(value: dict[str, object]) -> ReportedUsage | None:
    usage = value.get("usage")
    if not object_fields(usage):
        return None
    incoming, outgoing = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if (not isinstance(incoming, int) or isinstance(incoming, bool) or incoming < 0
            or not isinstance(outgoing, int) or isinstance(outgoing, bool) or outgoing < 0):
        return None
    return ReportedUsage(input_tokens=incoming, output_tokens=outgoing)


class SingleAttemptPolicy(LocalRequestPolicy):
    max_attempts = 1


def run_live(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    if (home / "metrics.json").exists() or (home / "failure.json").exists():
        raise ValueError("refusing to overwrite earlier live evidence")
    rows: list[dict[str, object]] = []
    for case in CASES:
        baseline, hint, result = fixture(home / "fixtures" / case.name, case)
        for question_index, question in enumerate(QUESTIONS):
            arms = (False, True) if question_index % 2 == 0 else (True, False)
            for enabled in arms:
                name = f"{case.name}-{question_index}-{'on' if enabled else 'off'}"
                admission_audit: list[dict[str, object]] = []
                prompt = render_request(baseline + "\n" + question, hint if enabled else "",
                                        scope="user", as_of=THIRD, audit=admission_audit)
                (home / (name + "-prompt.txt")).write_text(prompt, encoding="utf-8")
                provider = ApiProvider(
                    ENDPOINT, MODEL, temperature=0, timeout=120, native_tools=False,
                    context_window_tokens=262144, context_reserve_tokens=32768,
                    context_keep_recent_tokens=32000, output_tokens=256, usage_parser=parse_usage,
                    request_policy=SingleAttemptPolicy(),
                )
                usage_rows: list[dict[str, object]] = []

                def retain_usage(
                    record: ApiExchangeUsage, *, rows: list[dict[str, object]] = usage_rows,
                ) -> None:
                    rows.append(record.to_payload())

                provider.bind_usage(name, retain_usage)
                started = time.monotonic()
                try:
                    turn = provider.send_turn(prompt)
                    reply = turn.text
                    response: dict[str, object] = {
                        "reply": reply, "reasoning": turn.reasoning, "finish": turn.finish.value,
                        "raw": dict(turn.raw), "provider_usage": usage_rows,
                    }
                    (home / (name + "-response.json")).write_text(json.dumps(response, indent=2), encoding="utf-8")
                    if turn.finish != TurnFinish.COMPLETE or turn.tool_calls:
                        raise ValueError("non-complete or unexpected tool response")
                    if not reply.strip():
                        raise ValueError("empty model response")
                except Exception as error:
                    failure: dict[str, object] = {
                        "cell": name, "exception_type": type(error).__name__,
                        "message": str(error), "completed_cells": len(rows), "status": "INCOMPLETE",
                    }
                    (home / "failure.json").write_text(json.dumps(failure, indent=2), encoding="utf-8")
                    raise
                finally:
                    provider.close()
                selected = action(reply)
                row: dict[str, object] = {
                    "cell": name, "case": case.name, "enabled": enabled, "question": question_index,
                    "action": selected, "correct": selected == case.expected,
                    "review": result.outcome, "prompt_chars": len(prompt),
                    "hint_chars": len(hint) if enabled else 0, "seconds": time.monotonic() - started,
                    "prompt_digest": digest(prompt),
                }
                rows.append(row)
                sample: dict[str, object] = {
                    "prompt": prompt, "reply": reply, "reasoning": turn.reasoning,
                    "metrics": row, "provider_usage": usage_rows, "admission": admission_audit,
                }
                (home / (name + ".json")).write_text(json.dumps(sample, indent=2), encoding="utf-8")
                (home / "metrics.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
                print(json.dumps(row), flush=True)


class Options(argparse.Namespace):
    output: Path
    live: bool


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    options = Options()
    parser.parse_args(namespace=options)
    output = options.output
    if options.live:
        run_live(output)
    else:
        for case in CASES:
            base, hint, result = fixture(output / case.name, case)
            prompt = render_request(base, hint, scope="user", as_of=THIRD)
            summary: dict[str, object] = {
                "case": case.name, "review": result.outcome, "prompt_digest": digest(prompt),
            }
            print(json.dumps(summary))


if __name__ == "__main__":
    main()
