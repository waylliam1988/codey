"""Actual wire and first-response boundaries for the structured contrast."""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from codey.providers.api_transport import GenerationRejectedError
from codey.types.json_value import JsonObject, decode_json_object, decode_json_value
from codey.utils.object_fields import object_fields
from tests.manual.ghost_memory_routing import world_model_verification_review as original
from tests.manual.ghost_memory_routing.world_model_structured_review import response_format, run_structured


@contextmanager
def _endpoint(reply: str, *, status: int = 200, finish: str = "stop") -> Generator[tuple[str, list[JsonObject]]]:
    payloads: list[JsonObject] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            raw: object = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            payloads.append(decode_json_object(raw))
            body: dict[str, object] = {
                "choices": [{"message": {"role": "assistant", "content": reply}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            }
            if status != 200:
                body = {"error": {"message": "fixture response schema unsupported"}}
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", payloads
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


def _small_fixture(monkeypatch: pytest.MonkeyPatch, endpoint: str) -> None:
    monkeypatch.setattr(original, "ENDPOINT", endpoint)
    cases: tuple[original.Case, ...] = (original.CASES[0],)
    questions: tuple[str, ...] = (original.QUESTIONS[0],)
    monkeypatch.setattr(original, "CASES", cases)
    monkeypatch.setattr(original, "QUESTIONS", questions)


def _read(path: Path) -> JsonObject:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    return decode_json_object(raw)


def _metrics(path: Path) -> list[JsonObject]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    rows = decode_json_value(raw)
    assert isinstance(rows, list)
    return [decode_json_object(row) for row in rows]


def test_schema_is_exactly_the_original_scorer_action_contract() -> None:
    definition = response_format()
    assert definition["type"] == "json_schema"
    wrapper = definition["json_schema"]
    assert object_fields(wrapper) and wrapper["strict"] is True
    schema = wrapper["schema"]
    assert object_fields(schema) and schema["required"] == ["action"]
    assert schema["additionalProperties"] is False
    properties = schema["properties"]
    assert object_fields(properties) and set(properties) == {"action"}
    action = properties["action"]
    assert object_fields(action) and action["type"] == "string" and action["enum"] == list(original.ACTIONS)


@pytest.mark.parametrize("selected", original.ACTIONS)
def test_real_http_sends_same_schema_in_both_arms_and_does_not_execute_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selected: str,
) -> None:
    reply: dict[str, str] = {"action": selected}
    with _endpoint(json.dumps(reply)) as (endpoint, payloads):
        _small_fixture(monkeypatch, endpoint)
        run_structured(tmp_path)
    assert len(payloads) == 2
    assert all(row["response_format"] == response_format() for row in payloads)
    assert all("tools" not in row for row in payloads)
    assert "World Model prediction review" not in json.dumps(payloads[0])
    assert "World Model prediction review" in json.dumps(payloads[1])
    rows = _metrics(tmp_path / "metrics.json")
    assert len(rows) == 2 and all(row["action"] == selected for row in rows)
    assert all(row["correct"] == (selected == "inspect_environment") for row in rows)
    for row, captured in zip(rows, payloads, strict=True):
        cell = row["cell"]
        assert isinstance(cell, str)
        assert _read(tmp_path / (cell + "-wire-request.json")) == captured
        assert (tmp_path / (cell + "-response.json")).is_file()
        assert "wire_sha256" in row
    before = (tmp_path / "metrics.json").read_bytes()
    with pytest.raises(ValueError, match="overwrite"):
        run_structured(tmp_path)
    assert (tmp_path / "metrics.json").read_bytes() == before


@pytest.mark.parametrize("reply", (
    '```json\n{"action":"verify"}\n```', '{"action":"unknown"}',
    '{"action":"verify","extra":true}', '[]',
))
def test_first_invalid_structure_stops_interface_without_relaxing_original_scorer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: str,
) -> None:
    with _endpoint(reply) as (endpoint, payloads):
        _small_fixture(monkeypatch, endpoint)
        with pytest.raises(original.ReviewInterfaceError, match="original JSON interface"):
            run_structured(tmp_path)
    assert len(payloads) == 1
    failure = _read(tmp_path / "failure.json")
    assert failure["status"] == "INCOMPLETE_INTERFACE" and failure["completed_cells"] == 0
    assert not (tmp_path / "metrics.json").exists()
    saved = _read(tmp_path / "repeated_environment-0-off-response.json")
    assert saved["reply"] == reply
    assert original.action(reply).startswith("invalid_")


def test_original_unconstrained_interface_remains_unchanged_and_keeps_negative_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = '```json\n{"action":"inspect_environment"}\n```'
    with _endpoint(reply) as (endpoint, payloads):
        _small_fixture(monkeypatch, endpoint)
        original.run_live(tmp_path)
    assert len(payloads) == 2 and all("response_format" not in row for row in payloads)
    rows = _metrics(tmp_path / "metrics.json")
    assert len(rows) == 2 and all(row["action"] == "invalid_json" and row["correct"] is False for row in rows)


def test_invalid_fixture_stops_before_model_request_and_preserves_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid_fixture(home: Path, case: original.Case) -> tuple[str, str, original.Review]:
        raise ValueError("committed current event does not round-trip")

    with _endpoint('{"action":"verify"}') as (endpoint, payloads):
        _small_fixture(monkeypatch, endpoint)
        monkeypatch.setattr(original, "fixture", invalid_fixture)
        with pytest.raises(ValueError, match="does not round-trip"):
            run_structured(tmp_path)
    assert payloads == []
    saved = _read(tmp_path / "failure.json")
    assert saved["status"] == "INCOMPLETE" and saved["completed_cells"] == 0
    assert saved["cell"] == "repeated_environment-fixture"
    assert not (tmp_path / "metrics.json").exists()


@pytest.mark.parametrize("failure", ("http_error", "empty", "truncated"))
def test_service_or_incomplete_response_is_saved_as_incomplete_without_retry_or_negative_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    reply = "" if failure == "empty" else '{"action":"verify"}'
    with _endpoint(reply, status=400 if failure == "http_error" else 200,
                   finish="length" if failure == "truncated" else "stop") as (endpoint, payloads):
        _small_fixture(monkeypatch, endpoint)
        expected_error = GenerationRejectedError if failure == "http_error" else ValueError
        with pytest.raises(expected_error):
            run_structured(tmp_path)
    assert len(payloads) == 1
    saved = _read(tmp_path / "failure.json")
    assert saved["status"] == "INCOMPLETE" and saved["completed_cells"] == 0
    assert not (tmp_path / "metrics.json").exists()
    assert (tmp_path / "repeated_environment-0-off-wire-request.json").is_file()
