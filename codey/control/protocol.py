"""One bounded command catalogue for HTTP, CLI, RPC and MCP."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1024 * 1024
MAX_TEXT = 64_000


class ControlError(ValueError):
    def __init__(self, code: str, message: str, *, details: object = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "details": self.details}


@dataclass(frozen=True)
class Actor:
    actor_id: str
    name: str
    operator: bool = False


@dataclass(frozen=True)
class Command:
    description: str
    fields: dict[str, str]
    required: tuple[str, ...] = ()
    readonly: bool = True
    operator: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {key: {"type": kind} for key, kind in self.fields.items()},
            "required": list(self.required),
        }


S = {"conversation_id": "string"}
R = {**S, "run_id": "string"}
W = {**S, "request_id": "string", "control_generation": "integer"}
COMMANDS = {
    "get_health": Command("Read service, execution and release health.", {}),
    "list_conversations": Command("List accessible persistent conversations.", {}),
    "create_conversation": Command("Create a visible conversation in an authorized project.", {
        "request_id": "string", "project": "string", "title": "string", "provider": "string",
        "model_selection": "object", "max_revisions": "integer", "max_seconds": "integer",
    }, ("request_id",), False),
    "read_conversation": Command("Read one consistent history, inbox and run snapshot.", S, ("conversation_id",)),
    "get_control": Command("Read control owner, generation and remaining delegation budget.", S, ("conversation_id",)),
    "grant_control": Command("Explicitly delegate this conversation to the configured external actor.", {
        **W, "max_revisions": "integer", "max_seconds": "integer",
    }, ("conversation_id", "request_id", "control_generation"), False, True),
    "pause_control": Command("Revoke the current generation and cancel its pending work.", W,
                             ("conversation_id", "request_id", "control_generation"), False),
    "send_message": Command("Durably admit a task, correction or targeted steering input.", {
        **W, "text": "string", "mode": "string", "expected_run_id": "string",
        "in_reply_to_run_id": "string", "max_turns": "integer", "intent": "string",
    }, ("conversation_id", "request_id", "control_generation", "text"), False),
    "get_run_result": Command("Read the actual task status, answer, verification and evidence references.", R,
                              ("conversation_id", "run_id")),
    "wait_run": Command("Wait for terminal/attention or a change; timeout never cancels execution.", {
        **R, "until": "string", "after_revision": "string", "seen_attention_token": "string",
        "timeout_seconds": "number",
    }, ("conversation_id", "run_id")),
    "read_run_evidence": Command("Read a bounded page of run-owned history or execution output.", {
        **R, "execution_id": "string", "offset": "integer", "limit": "integer",
    }, ("conversation_id", "run_id")),
    "stop_run": Command("Request cancellation of exactly this run (cleanup may remain pending).", {
        **W, "run_id": "string",
    }, ("conversation_id", "run_id", "request_id", "control_generation"), False),
    "answer_question": Command("Answer a pending owner question without enlarging its task authorization.", {
        **W, "run_id": "string", "question_id": "string", "revision": "integer", "answer": "string",
    }, ("conversation_id", "run_id", "request_id", "control_generation", "question_id", "revision", "answer"), False),
    "record_review": Command("Record a candidate-bound independent review without starting model work.", {
        **W, "run_id": "string", "candidate_digest": "string", "passed": "boolean", "summary": "string",
        "verification": "array",
    }, ("conversation_id", "run_id", "request_id", "control_generation", "candidate_digest", "passed", "summary"), False),
    "prepare_release": Command("Snapshot and validate an immutable local Codey release candidate.", {
        "request_id": "string", "source": "string", "verification": "array",
    }, ("request_id", "source"), False, True),
    "activate_release": Command("Activate a validated release through the independent lifecycle coordinator.", {
        "request_id": "string", "candidate_release": "string", "candidate_digest": "string",
        "expected_instance_id": "string", "timeout_seconds": "number",
    }, ("request_id", "candidate_release", "candidate_digest", "expected_instance_id"), False, True),
    "get_upgrade": Command("Read durable release-coordinator status, including failed rollback.", {
        "upgrade_id": "string",
    }, ("upgrade_id",)),
}


def fingerprint(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def validate_command(name: str, params: object) -> dict[str, Any]:
    command = COMMANDS.get(name)
    if command is None:
        raise ControlError("INVALID_ARGUMENT", f"Unknown control operation: {name}")
    if not isinstance(params, dict) or set(params) - set(command.fields):
        raise ControlError("INVALID_ARGUMENT", "Expected an object containing only declared fields")
    if set(command.required) - set(params):
        raise ControlError("INVALID_ARGUMENT", "Missing required command fields")
    types = {"string": str, "object": dict, "array": list, "boolean": bool, "integer": int}
    for key, value in params.items():
        kind = command.fields[key]
        valid = type(value) in (int, float) if kind == "number" else type(value) is types[kind]
        if not valid:
            raise ControlError("INVALID_ARGUMENT", f"Invalid {key}: expected {kind}")
        if isinstance(value, str) and (len(value) > MAX_TEXT or (key.endswith("_id") and not value.strip())):
            raise ControlError("INVALID_ARGUMENT", f"Invalid length for {key}")
    try:
        size = len(json.dumps(params, ensure_ascii=False, allow_nan=False).encode())
    except (TypeError, ValueError) as exc:
        raise ControlError("INVALID_ARGUMENT", "Arguments must be finite JSON") from exc
    if size > MAX_FRAME_BYTES:
        raise ControlError("INVALID_ARGUMENT", "Command exceeds the frame budget")
    return dict(params)
