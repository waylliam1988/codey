"""Real HTTP/SSE regressions for the portability experiment's wire contract."""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import closing
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import pytest

from codey.ghost.payload_types import decoded_object
from codey.providers.token_accounting import ApiExchangeUsage
from tests.manual.ghost_memory_routing import portable_context_trial as trial

ANSWER = {"choice": "opalpanel", "reason": "offline_cursor_sync", "priority": "keyboard", "tone": "warm"}


@dataclass
class WireEndpoint:
    url: str
    omit_usage: bool = False
    requests: list[dict[str, object]] = field(default_factory=list)


@pytest.fixture()
def wire_endpoint() -> Generator[WireEndpoint]:
    endpoint = WireEndpoint("")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            payload = decoded_object(self.rfile.read(int(self.headers["Content-Length"])).decode())
            endpoint.requests.append(payload)
            arguments_payload: dict[str, str] = {"summary": json.dumps(ANSWER)}
            arguments = json.dumps(arguments_payload)
            call: dict[str, object] = {"id": "done-1", "type": "function",
                                       "function": {"name": "done", "arguments": arguments}}
            usage = {"prompt_tokens": 13, "completion_tokens": 3}
            response: dict[str, object] = {"choices": [{"message": {
                "role": "assistant", "content": None, "tool_calls": [call]}, "finish_reason": "tool_calls"}]}
            if not endpoint.omit_usage:
                response["usage"] = usage
            content_type = "application/json"
            body = json.dumps(response).encode()
            if payload.get("stream") is True:
                content_type = "text/event-stream"
                chunk: dict[str, object] = {"choices": [{"delta": {"tool_calls": [{"index": 0, **call}]},
                                                          "finish_reason": "tool_calls"}]}
                events = [chunk]
                if payload.get("stream_options") == {"include_usage": True} and not endpoint.omit_usage:
                    events.append({"choices": [], "usage": usage})
                body = ("".join("data: " + json.dumps(event) + "\n\n" for event in events)
                        + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        endpoint.url = f"http://127.0.0.1:{server.server_port}/v1"
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield endpoint
        finally:
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()


@pytest.mark.parametrize("stream", (False, True))
def test_usage_reaches_both_observers_over_real_http(
    tmp_path: Path, wire_endpoint: WireEndpoint, stream: bool,
) -> None:
    records: list[ApiExchangeUsage] = []
    with patch.object(trial, "ENDPOINT", wire_endpoint.url), closing(
        trial.CapturedProvider(tmp_path, output=8192, stream=stream)
    ) as provider:
        provider.bind_usage("fixture", records.append)
        turn = provider.send_turn("Return the fixture result.")
        assert turn.tool_calls and turn.tool_calls[0].name == "done"
        assert provider.requests == 1 and provider.usage_records == records
    assert len(records) == 1 and records[0].usage_status == "reported"
    assert records[0].usage.input_tokens == 13 and records[0].usage.output_tokens == 3
    assert (wire_endpoint.requests[0].get("stream_options") == {"include_usage": True}) == stream
    events = [decoded_object(line) for line in (tmp_path / "response-events.jsonl").read_text().splitlines()]
    assert any(event.get("usage") == {"prompt_tokens": 13, "completion_tokens": 3} for event in events)
    if stream:
        assert any(event.get("choices") == [] and "usage" in event for event in events)


def test_missing_usage_remains_unknown_without_retry(tmp_path: Path, wire_endpoint: WireEndpoint) -> None:
    wire_endpoint.omit_usage = True
    with patch.object(trial, "ENDPOINT", wire_endpoint.url), closing(
        trial.CapturedProvider(tmp_path, output=8192, stream=True)
    ) as provider:
        provider.bind_usage("fixture", lambda record: None)
        provider.send_turn("Return the fixture result.")
        assert provider.requests == 1 and len(provider.usage_records) == 1
        record = provider.usage_records[0]
    assert record.usage_status == "missing"
    assert record.usage.input_tokens is None and record.usage.output_tokens is None


@pytest.mark.parametrize("stream", (False, True))
def test_revised_cell_consumes_context_and_records_usage(
    tmp_path: Path, wire_endpoint: WireEndpoint, stream: bool,
) -> None:
    with patch.object(trial, "ENDPOINT", wire_endpoint.url):
        row = trial.cell(tmp_path / "cell", output=8192, stream=stream, arm="on", protocol="labels-v2")
    assert row["protocol"] == "labels-v2" and row["format_valid"] is True
    assert row["recall_correct"] is True and row["preference_correct"] is True
    assert row["sources_present"] is True and row["usage_status"] == "reported"
    assert row["input_tokens"] == 13 and row["generated_tokens"] == 3
    assert len(wire_endpoint.requests) == 1


def test_strict_scores_do_not_reinterpret_historical_phrases() -> None:
    phrase = {**ANSWER, "reason": "offline_cursor_sync preserves the project interface cursor",
              "tone": "warm and considerate"}
    scores = trial.score_answer(json.dumps(phrase), arm="on")
    assert scores["choice_correct"] and scores["priority_correct"]
    assert not scores["format_valid"] and not scores["reason_correct"]
    assert not scores["recall_correct"] and not scores["preference_correct"]


@pytest.mark.parametrize("answer", ({**ANSWER, "extra": "warm"}, {**ANSWER, "tone": 1}, {"choice": "opalpanel"}))
def test_label_contract_rejects_extra_missing_or_wrong_type_fields(answer: dict[str, object]) -> None:
    assert not trial.score_answer(json.dumps(answer), arm="on")["format_valid"]


def test_baseline_and_current_override_are_scored_separately() -> None:
    baseline = {"choice": "unknown", "reason": "unknown", "priority": "unknown", "tone": "neutral"}
    baseline_scores = trial.score_answer(json.dumps(baseline), arm="off")
    assert baseline_scores["format_valid"] and baseline_scores["baseline_unknown"]
    assert not baseline_scores["recall_correct"] and not baseline_scores["preference_correct"]
    override_answer: dict[str, str] = {**ANSWER, "priority": "animations", "tone": "technical"}
    override = trial.score_answer(json.dumps(override_answer), arm="override")
    assert override["format_valid"] and override["priority_correct"] and override["preference_correct"]
