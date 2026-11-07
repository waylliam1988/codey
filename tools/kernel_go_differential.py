"""Build and execute the independent Go migration reference; no skip fallback.

This validates a bounded contract slice, not the entire future Go application.
Run with: python -m tools.kernel_go_differential
"""

from __future__ import annotations

import copy
import json
import math
import os
import random
import shutil
import struct
import subprocess
from dataclasses import asdict
from pathlib import Path

from codey.operations.project_completion_checks import _gap_for_latest_row
from codey.providers.context_ledger import digest
from codey.providers.request_codec import decode_provider_request, encode_provider_request
from codey.providers.response_record import decode_reply, encode_reply
from codey.providers.text_result_views import validate_text_results
from codey.research.passages import passage_ref
from codey.runtime.core.operation_payload import LEAVES, RuntimeOperationState
from codey.runtime.core.operation_reducer import RuntimeDecisionInputs, next_runtime_action
from codey.runtime.core.verification_observation import (
    CompleteVerification,
    decode_verification_observation,
    latest_verification_observations,
    verification_observations,
)
from codey.runtime.log.session_view import SessionView
from codey.types.json_value import JsonObject, canonical_json_object, require_json_string

ROOT = Path(__file__).resolve().parents[1]


def build_cases() -> list[JsonObject]:
    vectors = json.loads((ROOT / "tests/fixtures/kernel_go_contract_v1.json").read_text(encoding="utf-8"))
    cases: list[JsonObject] = []

    def add(operation: str, value: object) -> None:
        cases.append(canonical_json_object({"operation": operation, "value": value}))

    for request in vectors["provider_invocations"]:
        add("request", request)
        for timeout in (None, False, -1, 0, 0.0001, 30, "30"):
            variant = copy.deepcopy(request)
            variant["kwargs"]["timeout"] = timeout
            add("request", variant)
        for version in (1, True, 2, None):
            add("request", {**request, "schema_version": version})
        for key, value in (("unknown", 1), ("output_token_limit", 0), ("output_token_limit", 2**63 - 1),
                           ("output_token_limit", 2**63), ("tool_call_history_groups", True),
                           ("require_tool_call", False), ("tool_call_json", False), ("tool_call_schema", None),
                           ("delivery_commit", {}), ("tool_call_contract", "")):
            variant = copy.deepcopy(request)
            variant["kwargs"][key] = value
            add("request", variant)
    for request in ({"method": "unknown", "args": [], "kwargs": {}},
                    {"method": "send", "args": ["p"], "kwargs": {"text": "duplicate"}},
                    {"method": "send", "args": [False], "kwargs": {}},
                    {"method": "send_turn", "args": ["p", [{"name": "tool", "description": "", "parameters": []}]], "kwargs": {}},
                    {"method": "send_tool_results", "args": [[{"call_id": "c", "content": 2, "user_content": None}], None], "kwargs": {}},
                    {"method": "send_tool_results_bound", "args": [[], {}, []], "kwargs": {}},
                    {"method": "acknowledge_tool_results", "args": [[], None], "kwargs": {}}):
        add("request", request)
    for reply in vectors["replies"]:
        add("reply", reply)
        add("reply", {**reply, "unexpected": True})
    native = vectors["replies"][1]
    for key, value in (("finish", "unknown"), ("reasoning", False), ("raw", []), ("tool_calls", {})):
        add("reply", {**native, key: value})
    for case in vectors["text_spans"]:
        add("span", case)
        for key, value in (("start", True), ("end", 999), ("digest", "wrong"), ("ok", "true"),
                           ("tool", ""), ("unexpected", 0)):
            variant = copy.deepcopy(case)
            variant["spans"][0][key] = value
            add("span", variant)
        variant = copy.deepcopy(case)
        variant["spans"] *= 2
        add("span", variant)
    for case in vectors["passage_refs"]:
        add("passage", case["input"])
    for value in ("中文😀e\u0301<>&\u2028\u2029\n\t\b\f", None, True, 0, -0.0, 1.0, 1e-4, 1e-5,
                  1e15, 1e16, 1e20, 1e100, 1.2345678901234567, 2**80,
                  {"😀": "", "a": [None, False, 0, 1.0], "中文": "<>&"}):
        add("digest", value)
    for exponent in range(-20, 21):
        add("digest", [1.23456789 * 10**exponent, -1.0 * 10**exponent])
    random_bits = random.Random(20261107)
    for _ in range(256):
        value = struct.unpack(">d", random_bits.getrandbits(64).to_bytes(8, "big"))[0]
        if math.isfinite(value):
            add("digest", {"number": value, "line_separator": "\u2028", "html": "<>&"})
    for case in vectors["verification_gaps"]:
        add("verification", case["row"])
    base = vectors["verification_gaps"][0]["row"]
    for key, value in (("passed", False), ("passed", "true"), ("passed", None), ("exit_code", 1),
                       ("exit_code", True), ("workspace_revision", 0), ("workspace_revision", None),
                       ("workspace_revision", "7"), ("workspace_fingerprint", "")):
        add("verification", {**base, key: value})
    for remove in ("passed", "exit_code", "workspace_revision", "workspace_fingerprint"):
        add("verification", {k: v for k, v in base.items() if k != remove})
    add("verification", {**base, "workspace_revision": 2**80})
    for field in ("exit_code", "workspace_revision", "workspace_fingerprint"):
        latest = {key: value for key, value in base.items() if key != field}
        for rows in ([base, latest], [latest, base], [base, {**latest, "cwd": "other"}],
                     [base, {**latest, "revision": 4}], [base, {**latest, "passed": False}]):
            add("latest_verification", {"edit_revision": 3, "rows": rows})
    add("reduce", {"leaf": None})
    for leaf in sorted((*LEAVES, "unknown_leaf")):
        if leaf == "tool_delivery_pending":
            continue  # Requires the effect/batch slice; never pretend to implement it.
        for cancel, closing in ((False, False), (True, False), (True, True)):
            for work in ("", "sha256:" + "a" * 64):
                add("reduce", {"leaf": leaf, "driver": "writer", "work_ref": work, "request_ref": "request",
                               "cancel_requested": cancel, "closing_intent": closing})
    add("reduce", {"leaf": "kernel_settled", "question_answered": True, "work_ref": "work"})
    add("reduce", {"leaf": "provider_request_ready", "response_ref": "response", "request_ref": "request"})
    add("reduce", {"leaf": "provider_request_ready"})
    return cases


def python_result(case: JsonObject) -> JsonObject:
    operation = case["operation"]
    value = case["value"]
    try:
        output: object
        if operation == "request":
            output = encode_provider_request(decode_provider_request(value))
        elif operation == "reply":
            output = encode_reply(decode_reply(value))
        elif operation == "span":
            row = canonical_json_object(value)
            output = validate_text_results(require_json_string(row["text"], "text"), row["spans"])
        elif operation == "passage":
            row = canonical_json_object(value)
            source, evidence = row.get("source_version"), row.get("evidence_version")
            output = passage_ref(row["url"], row["text_hash"], row["start"], row["end"],
                                 source_version=tuple(source) if source else None,
                                 evidence_version=tuple(evidence) if evidence else None)
        elif operation == "verification":
            row = decode_verification_observation(value)
            succeeded = row is not None and isinstance(row.outcome, CompleteVerification) and row.outcome.succeeded
            raw = canonical_json_object(value)
            output = {"gap": _gap_for_latest_row(value, str(raw.get("command") or "").strip()), "succeeded": succeeded}
        elif operation == "digest":
            output = digest(value)
        elif operation == "latest_verification":
            raw = canonical_json_object(value)
            rows = verification_observations(raw["rows"])
            latest = latest_verification_observations(rows, raw["edit_revision"])
            output = [{"command": row.command, "cwd": row.cwd,
                       "gap": "" if isinstance(row.outcome, CompleteVerification) else row.outcome.gap,
                       "succeeded": isinstance(row.outcome, CompleteVerification) and row.outcome.succeeded}
                      for row in latest.values()]
        elif operation == "reduce":
            row = canonical_json_object(value)
            state = RuntimeOperationState("session", "run", "operation", "task", "", "provider", 10, 1,
                str(row["leaf"]), "now", "now", driver=str(row.get("driver", "")),
                work_ref=str(row.get("work_ref", "")), request_ref=str(row.get("request_ref", "")),
                response_ref=str(row.get("response_ref", ""))) if row.get("leaf") is not None else None
            output = asdict(next_runtime_action(SessionView(state, (), ()), RuntimeDecisionInputs(
                cancel_requested=row.get("cancel_requested") is True, closing_intent=row.get("closing_intent") is True,
                question_answered=row.get("question_answered") is True)))
        else:
            raise ValueError(f"unknown operation {operation}")
        return canonical_json_object({"ok": True, "value": output})
    except (ValueError, TypeError, KeyError, OverflowError):
        return {"ok": False}


def run() -> int:
    go = os.environ.get("GO_EXECUTABLE") or shutil.which("go")
    local = ROOT / ".e2e-artifacts/toolchains/go/bin/go.exe"
    if not go and local.is_file():
        go = str(local)
    if not go:
        raise SystemExit("Go is required: install Go or set GO_EXECUTABLE to its executable (no skip fallback)")
    executable = ROOT / ".e2e-artifacts" / ("go-contract.exe" if os.name == "nt" else "go-contract")
    executable.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GOTOOLCHAIN": "local"}
    subprocess.run([go, "build", "-o", str(executable), "."], cwd=ROOT / "tools/go_contract", env=env, check=True)
    subprocess.run([go, "vet", "./..."], cwd=ROOT / "tools/go_contract", env=env, check=True)
    subprocess.run([go, "test", "./..."], cwd=ROOT / "tools/go_contract", env=env, check=True)
    cases = build_cases()
    expected = [python_result(case) for case in cases]
    result = subprocess.run([str(executable)], input=json.dumps(cases, ensure_ascii=False, allow_nan=False),
                            encoding="utf-8", capture_output=True, check=True)
    actual = json.loads(result.stdout)
    if len(actual) != len(expected):
        raise AssertionError("Go dropped cases")
    for index, (py, other) in enumerate(zip(expected, actual, strict=True)):
        if py != other:
            raise AssertionError(f"case {index}: {cases[index]!r}\nPython: {py!r}\nGo: {other!r}")
    accepted = sum(row["ok"] is True for row in expected)
    version = subprocess.check_output([go, "version"], encoding="utf-8").strip()
    print(f"{version}; {len(cases)} differential cases passed ({accepted} accepted, {len(cases)-accepted} rejected)")
    return len(cases)


if __name__ == "__main__":
    run()
