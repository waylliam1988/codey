"""Captured pixels survive real receipts, frozen requests and API HTTP encoding."""
from __future__ import annotations

import base64
import json
import threading
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from codey.codemode.kernel import build_execute_turn_executor
from codey.durable import DurableSession, SQLiteDurableStore
from codey.operations.browser.turn_surface import offer_browser_tools
from codey.operations.kernel.kernel_execution import execute_turn
from codey.operations.kernel_receipts import restore_result_receipt
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolDefinition, ProviderToolResult
from codey.providers.context_ledger import digest
from codey.providers.request_codec import decode_provider_request, encode_provider_request
from codey.providers.requests import SendToolResultsRequest
from codey.providers.text_result_views import TextResultSpan
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.effects.effect_records import RuntimeEffectStore
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject, parse_json_object
from codey.types.tool_image import ToolImageRef
from tests.support.kernel_fixture import runtime
from tests.support.task_browser import browser_for


def test_observe_only_tasks_offer_native_vision_without_interaction_or_programming() -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "browser.observe"})))
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    assert {"browser_snapshot", "browser_screenshot", "browser_frame_list", "browser_locate"} <= set(snapshot.tool_names)
    assert "browser_click" not in snapshot.tool_names and "browser_press" not in snapshot.tool_names
    from codey.operations.provider_receipts import restore_snapshot, snapshot_description

    assert restore_snapshot(snapshot_description(snapshot)) == snapshot
    session.controller_denied = ("browser_screenshot",)
    denied = offer_browser_tools(build_turn_snapshot(session), session)
    assert "browser_screenshot" not in denied.inventory_names
    assert "browser_snapshot" in denied.tool_names


@pytest.fixture()
def capture(tmp_path: Path) -> Iterator[ToolResult]:
    browser = browser_for(lambda route: route.fulfill(content_type="text/html", body="<h1>Picture</h1><button>Continue</button>"))
    try:
        assert browser.navigate("https://example.com/picture").ok
        session = TaskSession(TaskPolicy(frozenset({"browser.observe"})), task_kind="chat")
        delegate = ExecutionDelegate(session=session, browser_session=browser, managed_outputs=ManagedOutputStore(tmp_path),
                                     session_id="s", run_id="r")
        result, ok, _ = delegate.execute(ToolCall("browser_screenshot", {}, "capture-call"))
        assert ok, result.model_text
        assert len(result.images) == 1
        yield result
    finally:
        browser.close()


def test_captured_bytes_and_frozen_provider_results_survive_cold_receipt(tmp_path: Path, capture: ToolResult) -> None:
    store = ManagedOutputStore(tmp_path)
    durable = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    try:
        durable.admit_input(conversation_id="s", request_id="request-r", task_id="r", text="Inspect the image",
                            provider_snapshot={"provider_id": "local"}, task_state={"task_kind": "chat"})
        durable.start_task("r", state={"task_kind": "chat"})
        mutations = RuntimeMutationLine(durable=durable)
        mutations.accept_operation(session_id="s", run_id="r", project="", provider_id="local", turn_budget=10,
                                   max_repair_rounds=1, task_kind="chat")
        mutations.mark_writer_running("s", "r", provider_id="local")
        sink = KernelEffectSink(mutations, session_id="s", run_id="r", provider_id="local", managed_outputs=store, durable=durable)
        sink.begin_turn([("image-effect", capture.call, 0)], turn=1)
        sink.settle("image-effect", True, result=capture)
        effect = RuntimeEffectStore(durable=durable).load_effects("s", "r")[0]
        restored = restore_result_receipt(effect, store=store, session_id="s", run_id="r")
        assert restored.images == capture.images
        assert any(artifact.metadata.get("kind") == "tool_image" for artifact in durable.artifacts_for_effect("image-effect"))
        request = SendToolResultsRequest((ProviderToolResult("capture-call", "captured", images=restored.images),), None)
        frozen = encode_provider_request(request)
        assert decode_provider_request(frozen) == request
        assert "data:image" not in json.dumps(frozen)
    finally:
        durable.close()


@contextmanager
def api_server(protocol: str, *, native: bool = True, tool_name: str = "browser_screenshot",
               tool_arguments: JsonObject | None = None) -> Generator[tuple[str, list[JsonObject]], None, None]:
    requests: list[JsonObject] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(parse_json_object(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8")))
            if protocol == "openai-completions":
                message: JsonObject = ({"role": "assistant", "tool_calls": [{"id": "capture-call", "type": "function",
                    "function": {"name": tool_name, "arguments": json.dumps(tool_arguments or {})}}]} if len(requests) == 1 and native else
                    {"role": "assistant", "content": "Image received"})
                body: JsonObject = {"choices": [{"message": message, "finish_reason": "stop"}]}
            else:
                body = {"status": "completed", "output": [{"type": "function_call", "call_id": "capture-call",
                    "name": tool_name, "arguments": json.dumps(tool_arguments or {})}] if len(requests) == 1 else
                    [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Image received"}]}]}
            raw = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def assert_pixels(request: JsonObject, image: ToolImageRef) -> None:
    encoded = json.dumps(request)
    prefix = "data:image/png;base64,"
    assert prefix in encoded
    data = encoded.split(prefix, 1)[1].split('"', 1)[0]
    original = ManagedOutputStore(image.state_home).read_binary_artifact(session_id=image.session_id, run_id=image.run_id,
        handle=image.handle, sha256=image.sha256, size=image.size)
    assert base64.b64decode(data) == original
    assert "state_home" not in encoded and "_codey_tool_images" not in encoded


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_native_http_requests_contain_the_original_pixels(capture: ToolResult, protocol: str) -> None:
    with api_server(protocol) as (base, requests):
        provider = ApiProvider(base, "vision-fixture", supports_images=True, native_tools=True, api_protocol=protocol)
        try:
            tools = [ProviderToolDefinition("browser_screenshot", "capture", {"type": "object", "properties": {}})]
            assert provider.send_turn("Inspect", tools).tool_calls
            provider.send_tool_results([ProviderToolResult("capture-call", "captured", images=capture.images)], tools)
            assert_pixels(requests[-1], capture.images[0])
        finally:
            provider.close()


def test_text_json_http_request_contains_pixels_without_serializing_binary_into_history(capture: ToolResult) -> None:
    with api_server("openai-completions", native=False) as (base, requests):
        provider = ApiProvider(base, "vision-fixture", supports_images=True, native_tools=False,
                               thinking_enabled=False, text_tool_format="json_schema")
        try:
            text = "The browser captured the current page."
            span = TextResultSpan(0, len(text), digest(text), "image-effect", "browser_screenshot", True, capture.images)
            provider.send(text, tool_call_results=[dict(span.to_payload())])
            assert_pixels(requests[-1], capture.images[0])
        finally:
            provider.close()


@pytest.mark.parametrize('protocol', ['openai-completions', 'openai-responses'])
def test_codemode_child_image_has_one_artifact_and_reaches_actual_http(protocol: str) -> None:
    browser = browser_for(lambda route: route.fulfill(content_type='text/html',
        body='<h1>CodeMode captured pixels</h1><button>Confirm</button>'), owner_task_id='r')
    args: JsonObject = {'code': 'unwrap(await tools.browser_screenshot({}));return null;'}
    try:
        assert browser.navigate('https://example.com/parent-image').ok
        with runtime(session_id='s', run_id='r', task_kind='chat') as sink:
            session = TaskSession(TaskPolicy(frozenset({'control', 'browser.observe'})), task_kind='chat')
            snapshot = build_turn_snapshot(session, native=True)
            executor = build_execute_turn_executor(session=session, snapshot=snapshot, session_id='s',
                run_id='r', turn=1, effect_scope='', browser_session=browser,
                durable=sink.durable, intent_sink=sink, managed_outputs=sink.managed_outputs)
            parent = execute_turn(session, [ToolCall('codemode', args, 'capture-call')],
                session_id='s', run_id='r', turn=1, snapshot=snapshot, browser_session=browser,
                executors={'codemode': executor},
                durable=sink.durable, intent_sink=sink, managed_outputs=sink.managed_outputs)[0]
            assert parent.ok and len(parent.images) == 1, parent
            child = next(row for row in session._memory_results.values() if row.call.name == 'browser_screenshot')
            assert child.images == parent.images
            artifacts = [row for row in sink.durable.artifacts_for_task('r') if row.metadata.get('kind') == 'tool_image']
            assert len(artifacts) == 1 and artifacts[0].owner_effect_id != next(
                ref for ref, row in session._memory_results.items() if row is parent)
            with api_server(protocol, tool_name='codemode', tool_arguments=args) as (base, requests):
                provider = ApiProvider(base, 'vision-fixture', supports_images=True, native_tools=True, api_protocol=protocol)
                try:
                    tools = [ProviderToolDefinition('codemode', 'composition',
                        {'type': 'object', 'properties': {'code': {'type': 'string'}}, 'required': ['code']})]
                    assert provider.send_turn('Inspect the page', tools).tool_calls
                    provider.send_tool_results([ProviderToolResult('capture-call', parent.model_text, images=parent.images)], tools)
                    assert_pixels(requests[-1], parent.images[0])
                finally:
                    provider.close()
    finally:
        browser.close()
