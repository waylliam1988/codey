"""Production child IPC, concurrency and cancellation with a real browser."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from codey.automation.browser_binding import BrowserBinding, browser_endpoint
from codey.automation.browser_contracts import (
    MAX_PENDING_COMMANDS,
    BrowserAction,
    BrowserCommand,
    BrowserConfig,
    BrowserToolOutput,
)
from codey.automation.browser_ipc import BrowserPacket
from codey.automation.browser_process import BrowserProcess, Pending, Progress
from codey.automation.browser_session import BrowserSession
from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject, decode_json_object
from tests.support.real_cdp import chromium_executable
from tests.test_browser_async_runtime import HTML


def route(request: JsonObject) -> JsonObject:
    return {"decision": "fulfill", "headers": {"content-type": "text/html"}, "body": HTML}


def command(action: BrowserAction, args: JsonObject | None = None) -> BrowserCommand:
    return BrowserCommand(uuid4().hex, action, args or {}, "task", "resource", "effect", time.monotonic() + 30)


@pytest.mark.parametrize("caller_budget", [False, True])
def test_facade_respects_worker_deadline_receipt_before_parent_clock_expires(
    monkeypatch: pytest.MonkeyPatch, caller_budget: bool,
) -> None:
    def expired(self: BrowserProcess, command: BrowserCommand, *, binary: bytes = b"",
                progress: Progress | None = None) -> BrowserPacket:
        assert command.deadline > time.monotonic()
        output = BrowserToolOutput("ERROR: deadline", False, {"reason": "deadline", "delivery": "uncertain"})
        return BrowserPacket({"completion": "failed", "value": output.payload()})
    monkeypatch.setattr(BrowserProcess, "call", expired)
    browser = BrowserSession()
    if caller_budget:
        with cancellation.deadline_scope(time.monotonic() + 10), pytest.raises(cancellation.DeadlineExceeded):
            browser.navigate("https://example.com/")
    else:
        assert not browser.navigate("https://example.com/").ok


def test_saturated_queue_rejects_the_new_command_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = BrowserProcess(BrowserConfig(), "task", "resource")
    monkeypatch.setattr(worker, "_start", lambda: None)
    worker._pending.update({str(index): Pending() for index in range(MAX_PENDING_COMMANDS)})
    result = BrowserToolOutput.read(worker.call(command(BrowserAction.CLICK, {"ref": "observed"})).message["value"])
    assert not result.ok and result.observation["reason"] == "browser_queue_full"
    assert result.observation["delivery"] == "not_delivered"
    assert len(worker._pending) == MAX_PENDING_COMMANDS
    assert worker._process is None


def test_close_services_inflight_network_replies_before_cleanup_acknowledgement() -> None:
    started = threading.Event()
    def delayed_policy(request: JsonObject) -> JsonObject:
        started.set()
        time.sleep(0.03)
        return {"decision": "abort", "reason": "fixture_denied"}
    worker = BrowserProcess(BrowserConfig(browser_path=chromium_executable()), "task", "resource",
                            network_policy=delayed_policy)
    try:
        opened = worker.call(command(BrowserAction.NAVIGATE, {"url": "about:blank"}))
        assert BrowserToolOutput.read(opened.message["value"]).ok
        code = "document.body.innerHTML=Array.from({length:100},(_,i)=>`<img src='http://127.0.0.1:9/${i}'>`).join(''); true"
        evaluated = worker.call(command(BrowserAction.EVALUATE, {"code": code}))
        assert BrowserToolOutput.read(evaluated.message["value"]).ok
        assert started.wait(3)
        worker.close()
        assert worker._closed is not None and worker._closed["ok"] is True
    finally:
        worker.close()


def test_worker_real_browser_wait_cancel_and_binary_capture() -> None:
    worker = BrowserProcess(BrowserConfig(browser_path=chromium_executable()), "task", "resource", network_policy=route)
    try:
        opened = worker.call(command(BrowserAction.NAVIGATE, {"url": "https://example.com/record"}))
        assert BrowserToolOutput.read(opened.message["value"]).ok
        assert worker.binding is not None
        stop = threading.Event()

        def wait() -> BrowserToolOutput:
            with cancellation.scope(stop):
                packet = worker.call(command(BrowserAction.WAIT, {"condition": "text", "value": "never"}))
                return BrowserToolOutput.read(packet.message["value"])

        with ThreadPoolExecutor(max_workers=1) as executor:
            waiting = executor.submit(wait)
            snap = worker.call(command(BrowserAction.SNAPSHOT))
            assert BrowserToolOutput.read(snap.message["value"]).ok
            stop.set()
            result = waiting.result(timeout=5)
            assert not result.ok and result.observation["reason"] == "cancelled"
        capture = worker.call(command(BrowserAction.SCREENSHOT))
        output = BrowserToolOutput.read(capture.message["value"], binary=capture.blob)
        assert output.ok, output.model_text
        assert capture.blob.startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        worker.close()


def test_worker_loss_keeps_the_effect_uncertain_and_reconnects_only_the_original_browser(tmp_path: Path) -> None:
    config = BrowserConfig(browser_path=chromium_executable(), profile_dir=str(tmp_path / "browser"))
    worker = BrowserProcess(config, "task", "resource", network_policy=route)
    recovered: BrowserProcess | None = None
    try:
        assert BrowserToolOutput.read(worker.call(command(BrowserAction.NAVIGATE, {"url": "https://example.com/record"})).message["value"]).ok
        snapshot = BrowserToolOutput.read(worker.call(command(BrowserAction.SNAPSHOT)).message["value"])
        rows = snapshot.observation["elements"]
        assert isinstance(rows, list)
        ref = next(row["ref"] for row in rows if isinstance(row, dict) and row["name"] == "Query")
        def lose_result(progress: JsonObject) -> None:
            if progress.get("phase") == "input_complete":
                process = worker._process
                assert process is not None
                process.kill()
        failed = worker.call(command(BrowserAction.CLICK, {"ref": ref}), progress=lose_result)
        outcome = BrowserToolOutput.read(failed.message["value"])
        assert not outcome.ok and outcome.observation["delivery"] == "uncertain"
        binding = worker.binding
        assert binding is not None
        worker.close(release=False)
        recovered = BrowserProcess(config, "task", "resource", network_policy=route, binding=binding)
        captured = decode_json_object(recovered.call(command(BrowserAction.CAPTURE)).message["value"])
        assert "173 QUARTZ_851" in str(captured["text"])
        assert recovered.binding is not None and recovered.binding.browser_id == binding.browser_id
        assert recovered.binding.active_target == binding.active_target
    finally:
        worker.close(release=False)
        if recovered is not None:
            recovered.close()


def test_startup_binding_is_recorded_before_page_creation_and_cold_cleanup_releases_browser() -> None:
    config = BrowserConfig(browser_path=chromium_executable())
    worker = BrowserProcess(config, "task", "resource", network_policy=route)
    startup: list[BrowserBinding] = []

    def observe(progress: JsonObject) -> None:
        if progress.get("phase") == "browser_connected":
            startup.append(BrowserBinding.read(progress["binding"]))

    try:
        assert BrowserToolOutput.read(worker.call(command(BrowserAction.NAVIGATE,
            {"url": "https://example.com/record"}), progress=observe).message["value"]).ok
        assert len(startup) == 1 and not startup[0].active_target and not startup[0].pages
        binding = worker.binding
        assert binding is not None and binding.owned_browser and binding.owned_profile
        worker.close(release=False)
        assert browser_endpoint(binding.port)
        recovered = BrowserProcess(config, "task", "resource", network_policy=route, binding=binding)
        recovered.close()  # No command, page observation or replacement launch.
        assert not Path(binding.owned_profile).exists()
        with pytest.raises(OSError):
            browser_endpoint(binding.port)
    finally:
        worker.close(release=False)


def test_worker_loss_during_chord_releases_recorded_keys_on_exact_reconnect() -> None:
    worker = BrowserProcess(BrowserConfig(browser_path=chromium_executable()), "task", "resource", network_policy=route)
    try:
        assert BrowserToolOutput.read(worker.call(command(BrowserAction.NAVIGATE,
            {"url": "https://example.com/record"})).message["value"]).ok
        assert BrowserToolOutput.read(worker.call(command(BrowserAction.EVALUATE, {'code':
            "(() => {document.querySelector('input').onkeydown=e=>document.querySelector('#result').textContent=String(e.shiftKey);return true;})()"})).message['value']).ok
        snap = BrowserToolOutput.read(worker.call(command(BrowserAction.SNAPSHOT)).message['value'])
        rows = snap.observation['elements']
        assert isinstance(rows, list)
        city = next(decode_json_object(row)['ref'] for row in rows if decode_json_object(row)['name'] == 'City')

        def lose_worker(progress: JsonObject) -> None:
            inputs = progress.get('input_state')
            if progress.get('phase') == 'key_input' and isinstance(inputs, dict) and inputs.get('keys') == ['Shift', 'A']:
                process = worker._process
                assert process is not None
                process.kill()

        failed = BrowserToolOutput.read(worker.call(command(BrowserAction.PRESS, {'ref': city, 'key': 'Shift+A'}),
            progress=lose_worker).message['value'])
        assert not failed.ok and failed.observation['delivery'] == 'uncertain'
        original = worker.binding
        snap = BrowserToolOutput.read(worker.call(command(BrowserAction.SNAPSHOT)).message['value'])
        assert snap.ok and worker.binding == original
        rows = snap.observation['elements']
        assert isinstance(rows, list)
        city = next(decode_json_object(row)['ref'] for row in rows if decode_json_object(row)['name'] == 'City')
        assert BrowserToolOutput.read(worker.call(command(BrowserAction.PRESS, {'ref': city, 'key': 'a'})).message['value']).ok
        captured = decode_json_object(worker.call(command(BrowserAction.CAPTURE)).message['value'])
        assert 'false' in str(captured['text'])
    finally:
        worker.close()
