"""Real accepted receipt requests remain uncertain after incomplete responses."""

from __future__ import annotations

import json
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from codey.runtime.effects.effect_records import RuntimeEffectProjection
from codey.types.json_value import decode_json_object
from tests.test_ghost_native_refusal_recovery import CALL, RUN, Capture, Runtime, _reply


@pytest.fixture()
def uncertain_endpoint(protocol: str, fault: str) -> Generator[Capture]:
    observed = Capture("")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            assert self.path == ("/v1/responses" if protocol == "openai-responses" else "/v1/chat/completions")
            raw: object = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            observed.payloads.append(decode_json_object(raw))
            initial = len(observed.payloads) == 1
            if not initial and fault == "eof_before_headers":
                self.close_connection = True
                return
            body = json.dumps(_reply(protocol, initial=initial)).encode()
            if not initial and fault == "invalid_json":
                body = b'{"unfinished":'
            declared = len(body) + (17 if not initial and fault == "short_frame_valid_json" else 0)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(declared))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        observed.endpoint = f"http://127.0.0.1:{server.server_port}/v1"

        def serve() -> None:
            server.serve_forever(poll_interval=0.02)

        worker = Thread(target=serve, daemon=True)
        worker.start()
        try:
            yield observed
        finally:
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()


def _provider_failures(runtime: Runtime) -> list[RuntimeEffectProjection]:
    return [row for row in runtime.effects() if row.intent.effect_category == "provider_send"
            and row.settlement is not None and row.settlement.status == "error"]


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
@pytest.mark.parametrize("scope", ["user", "project", "session"])
@pytest.mark.parametrize("ok", [True, False], ids=["ok", "error"])
@pytest.mark.parametrize("restart", ["warm", "cold"])
@pytest.mark.parametrize("fault", ["eof_before_headers", "invalid_json", "short_frame_valid_json"])
def test_received_receipt_with_uncertain_response_is_never_accepted_or_resent(
    tmp_path: Path, uncertain_endpoint: Capture, protocol: str, scope: str, ok: bool,
    restart: str, fault: str,
) -> None:
    runtime = Runtime(tmp_path, uncertain_endpoint, protocol, scope, ok, "enabled")
    try:
        result = runtime.run()
        assert not result.completed and result.stop_reason == "provider_failure", (fault, result)
        assert len(uncertain_endpoint.payloads) == 2 and len(runtime.executed) == 1
        [failure] = _provider_failures(runtime)
        assert failure.settlement is not None
        assert failure.settlement.sent_state == "maybe_sent"
        assert failure.settlement.error_code == "GenerationUnknownError"
        [batch] = runtime.batches()
        assert not batch.is_terminal and not batch.is_delivered
        assert batch.active_attempts == (failure.intent.effect_id,)
        assert not batch.superseded_effect_ids
        original_effects = runtime.durable.effects(RUN)
        [tool] = [row for row in runtime.effects() if row.intent.tool_name == "read"]
        assert tool.settlement is not None and tool.settlement.call_id == CALL
        assert tool.settlement.status == ("ok" if ok else "error")
        assert ("ORIGINAL_RECEIPT_OK" if ok else "ORIGINAL_RECEIPT_ERROR") in tool.settlement.result_text
        for enabled in (False, True):
            runtime.control(enabled)
            if restart == "cold":
                runtime.reopen()
                assert runtime.enabled() == enabled
            resumed = runtime.run()
            assert not resumed.completed and resumed.stop_reason == "recovery_failure", resumed
            assert "external_outcome_unknown" in resumed.summary, resumed
            assert len(uncertain_endpoint.payloads) == 2 and len(runtime.executed) == 1
            assert runtime.durable.effects(RUN) == original_effects
            assert runtime.batches() == (batch,)
    finally:
        runtime.close()
