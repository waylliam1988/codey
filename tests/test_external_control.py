"""External transports must observe the existing durable execution owner."""

from __future__ import annotations

import asyncio
import copy
import json
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from codey.app.public_event_feed import FeedOverflow, PublicEventFeed
from codey.app.service import LocalService
from codey.control.client import ControlClient
from codey.control.protocol import ControlError


@pytest.fixture
def service(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    runtime = LocalService(tmp_path / "state", allowed_roots=(root,), verify_only=True).start()
    # Admission contract tests deliberately leave the unique scheduler stopped.
    # Real dispatch and process transitions have separate live/process tests.
    runtime.verify_only = False
    runtime.control.draining = False
    runtime.control.verifying = False
    runtime._publish()
    yield runtime, ControlClient(runtime.home), root
    runtime.close()


def create(client: ControlClient, root: Path, request_id: str = "create-1") -> dict:
    return client.call("create_conversation", {"request_id": request_id, "project": str(root)})


def send(client: ControlClient, config: dict, request_id: str = "send-1", text: str = "Inspect this folder read only.") -> dict:
    return client.call("send_message", {"conversation_id": config["conversation_id"], "request_id": request_id,
                                       "control_generation": config["control_generation"], "text": text})


def test_visible_creation_and_original_receipt_are_idempotent(service) -> None:
    runtime, client, root = service
    config = create(client, root)
    assert create(client, root) == config
    assert config["conversation_id"] in {item["id"] for item in runtime.ctx.load_ui_state()["sessions"]}
    admitted = send(client, config)
    assert send(client, config)["run_id"] == admitted["run_id"]
    assert len(runtime.ctx.durable.tasks()) == 1
    snapshot = client.call("read_conversation", {"conversation_id": config["conversation_id"]})
    user = next(item for item in snapshot["history"]["items"] if item["type"] == "user")
    assert user["source"]["name"] == "Codex"
    assert all(item["type"] not in {"control_config", "control_receipt"} for item in snapshot["history"]["items"])


def test_external_model_selection_has_a_connection_identity_in_the_desktop(service):
    runtime, client, root = service
    config = client.call("create_conversation", {"request_id": "model-visible", "project": str(root),
        "provider": "zen", "model_selection": {"model": "big-pickle"}})
    item = next(item for item in runtime.ctx.load_ui_state()["sessions"] if item["id"] == config["conversation_id"])
    assert item["modelSelection"]["connection_id"] == "zen"
    assert item["modelSelection"]["model"] == "big-pickle"


def test_inventory_rebuild_preserves_an_existing_user_title(service):
    from codey.app.control_conversations import publish_inventory

    runtime, client, root = service
    config = create(client, root)
    state = copy.deepcopy(runtime.ctx.load_ui_state())
    item = next(item for item in state["sessions"] if item["id"] == config["conversation_id"])
    item["title"] = "User renamed this chat"
    runtime.ctx.ui_state_store.save(state, base_revision=state["revision"])
    publish_inventory(runtime.ctx)
    item = next(item for item in runtime.ctx.load_ui_state()["sessions"] if item["id"] == config["conversation_id"])
    assert item["title"] == "User renamed this chat"


def test_lost_receipt_does_not_readmit_input(service, monkeypatch) -> None:
    from codey.durable import control

    runtime, client, root = service
    config = create(client, root)
    real = control.write
    fail = True

    def fail_after_admission(*args, **kwargs):
        nonlocal fail
        if args[3] == "send_message" and fail:
            fail = False
            raise OSError("simulated receipt write failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(control, "write", fail_after_admission)
    with pytest.raises(ControlError, match="failed"):
        send(client, config)
    recovered = send(client, config)
    assert len(runtime.ctx.durable.tasks()) == 1
    assert runtime.ctx.durable.tasks()[0].task_id == recovered["run_id"]


def test_content_conflict_and_revocation_survive_retries(service) -> None:
    runtime, client, root = service
    config = create(client, root)
    send(client, config)
    with pytest.raises(ControlError) as conflict:
        send(client, config, text="Different instructions")
    assert conflict.value.code == "REQUEST_CONFLICT"
    paused = client.call("pause_control", {"conversation_id": config["conversation_id"], "request_id": "pause-1",
                                            "control_generation": config["control_generation"]})
    assert paused["control_generation"] == config["control_generation"] + 1
    with pytest.raises(ControlError) as stale:
        send(client, config, "send-late")
    assert stale.value.code == "STALE_CONTROL"
    assert str(runtime.ctx.durable.tasks()[0].status) == "cancelled"


def test_wait_timeout_is_observation_not_cancellation(service) -> None:
    runtime, client, root = service
    config = create(client, root)
    admission = send(client, config)
    result = client.call("wait_run", {"conversation_id": config["conversation_id"], "run_id": admission["run_id"],
                                      "timeout_seconds": 0})
    assert result["reason"] == "timeout"
    assert result["snapshot"]["execution"] not in {"cancelled", "completed"}
    assert len(runtime.ctx.durable.tasks()) == 1


def test_project_scope_and_operator_capabilities(service, tmp_path) -> None:
    _, client, _ = service
    with pytest.raises(ControlError) as denied:
        create(client, tmp_path)
    assert denied.value.code == "FORBIDDEN"
    with pytest.raises(ControlError) as denied:
        client.call("prepare_release", {"request_id": "release-1", "source": str(tmp_path)})
    assert denied.value.code == "FORBIDDEN"


def test_config_is_recovered_without_the_ui_cache(tmp_path: Path) -> None:
    home = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    runtime = LocalService(home, allowed_roots=(project,)).start()
    config = create(ControlClient(home), project)
    runtime.close()
    (home / "ui-state.json").unlink()
    successor = LocalService(home, allowed_roots=(project,)).start()
    try:
        assert ControlClient(home).call("get_control", {"conversation_id": config["conversation_id"]}) == config
        assert config["conversation_id"] in {item["id"] for item in successor.ctx.load_ui_state()["sessions"]}
    finally:
        successor.close()


def test_feed_encodes_once_and_isolates_slow_consumers() -> None:
    feed = PublicEventFeed()
    slow = feed.subscribe(max_events=1)
    healthy = [feed.subscribe() for _ in range(20)]
    feed.publish(1, {"type": "info", "text": "first", "session_id": "s"})
    for item in healthy:
        assert b"first" in item.get().body
    assert feed.encodes == 1
    feed.publish(2, {"type": "info", "text": "second", "session_id": "s"})
    with pytest.raises(FeedOverflow):
        slow.get()
    for item in healthy:
        assert b"second" in item.get().body
    assert feed.encodes == 2
    feed.close()


def test_feed_filters_before_queueing_and_limits_bytes() -> None:
    feed = PublicEventFeed()
    selected = feed.subscribe(sessions=frozenset({"mine"}), max_bytes=100)
    feed.publish(1, {"type": "info", "text": "secret", "session_id": "other"})
    assert not selected.items
    feed.publish(2, {"type": "info", "text": "x" * 200, "session_id": "mine"})
    with pytest.raises(FeedOverflow):
        selected.get()


def test_internal_events_never_escape_the_public_feed() -> None:
    feed = PublicEventFeed()
    selected = feed.subscribe()
    feed.publish(1, {"type": "private_diagnostic", "secret": "never broadcast"})
    with pytest.raises(queue.Empty):
        selected.get(timeout=0)
    feed.publish(2, {"type": "conversation_inventory_changed", "secret": "also private"})
    assert json.loads(selected.get().body) == {"type": "conversation_inventory_changed"}


def test_review_requires_a_settled_matching_candidate_and_becomes_stale(service):
    runtime, client, root = service
    config = create(client, root)
    run = send(client, config)["run_id"]
    args = {"conversation_id": config["conversation_id"], "run_id": run, "request_id": "review-1",
            "control_generation": 1, "passed": True, "summary": "Independent file check", "candidate_digest": "old"}
    with pytest.raises(ControlError) as error:
        client.call("record_review", args)
    assert error.value.code == "REVISION_CONFLICT"
    client.call("stop_run", {"conversation_id": config["conversation_id"], "run_id": run,
                              "request_id": "stop-1", "control_generation": 1})
    result = client.call("get_run_result", {"conversation_id": config["conversation_id"], "run_id": run})
    args["candidate_digest"] = result["candidate_digest"]
    client.call("record_review", args)
    (root / "new.py").write_text("changed = True\n")
    result = client.call("get_run_result", {"conversation_id": config["conversation_id"], "run_id": run})
    assert result["reviews"][0]["stale"]


def test_drain_closes_shell_dispatch_and_waits_for_approval_workers(service):
    runtime, _, _ = service
    runtime.ctx.shell_approval_workflow._workers["occupied"] = object()
    operator = ControlClient(runtime.home, operator=True)
    try:
        status = operator.lifecycle("drain")
        assert not status["quiescent"]
        with pytest.raises(ControlError):
            operator.lifecycle("shutdown")
    finally:
        runtime.ctx.shell_approval_workflow._workers.clear()


def test_pause_retry_reconciles_cancellation_after_committed_revocation(service, monkeypatch):
    runtime, client, root = service
    config = create(client, root)
    run = send(client, config)["run_id"]
    original = runtime.control._cancel_owned
    calls = 0

    def interrupted(config, cancel):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("lost process after generation barrier")
        return original(config, cancel)

    monkeypatch.setattr(runtime.control, "_cancel_owned", interrupted)
    args = {"conversation_id": config["conversation_id"], "request_id": "pause", "control_generation": 1}
    with pytest.raises(ControlError):
        client.call("pause_control", args)
    client.call("pause_control", args)
    assert str(runtime.ctx.durable.task(run).status) == "cancelled"


def test_reply_stream_state_is_preserved_by_public_encoding():
    feed = PublicEventFeed()
    client = feed.subscribe()
    feed.publish(1, {"type": "reply_stream", "stream_id": "s", "run_id": "r", "sequence": 1, "state": "partial", "text": "hello"})
    assert json.loads(client.get().body)["state"] == "partial"


def test_public_terminal_preserves_elapsed_time_and_research_navigation():
    feed = PublicEventFeed()
    subscriber = feed.subscribe()
    feed.publish(1, {"type": "task_done", "run_id": "r", "session_id": "s", "stop_reason": "done",
        "elapsed_ms": 1234, "research": {"synthesis_id": "note-1", "notes_created": ["fact-1"],
                                       "restoreable": True, "secret_internal": "omit"}})
    event = json.loads(subscriber.get().body)
    assert event["elapsed_ms"] == 1234
    assert event["research"] == {"synthesis_id": "note-1", "notes_created": ["fact-1"], "restoreable": True}


def test_large_research_details_are_an_explicit_bounded_preview():
    feed = PublicEventFeed()
    subscriber = feed.subscribe()
    feed.publish(1, {"type": "task_done", "research": {"evidence_items": [{"content": "evidence" * 100000}]}})
    body = subscriber.get().body
    assert len(body) < 128 * 1024
    assert json.loads(body)["research_truncated"] is True


def test_disabled_model_rejects_before_input_admission(service, monkeypatch):
    runtime, client, root = service
    config = create(client, root)
    monkeypatch.setattr(runtime.ctx.providers.model_preferences, "allows", lambda *_: False)
    with pytest.raises(ControlError) as error:
        send(client, config)
    assert error.value.code == "MODEL_DISABLED"
    assert not runtime.ctx.durable.tasks()


def test_api_default_is_frozen_before_the_input_is_committed(service, monkeypatch):
    from codey.runtime.core.api_selection import ApiRunSelection

    runtime, client, root = service
    selection = ApiRunSelection("zen", "test-contract", "chosen-default", "openai-completions", True)
    monkeypatch.setattr("codey.providers.snapshot.capture_provider_selection", lambda *_: selection)
    monkeypatch.setattr(runtime.ctx.providers.model_preferences, "allows", lambda *_: True)
    config = client.call("create_conversation", {"request_id": "api-create", "project": str(root), "provider": "zen"})
    admitted = send(client, config)
    task = runtime.ctx.durable.task(admitted["run_id"])
    assert task.provider_snapshot["model_selection"]["model_id"] == "chosen-default"


def test_slow_model_selection_does_not_hold_the_durable_mutation_line(service, monkeypatch):
    runtime, client, root = service
    config = create(client, root)
    entered, release, observed = threading.Event(), threading.Event(), threading.Event()

    def slow_selection(*_args):
        entered.set()
        release.wait(3)
        return None

    monkeypatch.setattr("codey.providers.snapshot.capture_provider_selection", slow_selection)
    worker = threading.Thread(target=lambda: send(client, config))
    worker.start()
    assert entered.wait(2)
    observer = threading.Thread(target=lambda: (client.call("get_control", {"conversation_id": config["conversation_id"]}), observed.set()))
    observer.start()
    try:
        assert observed.wait(1), "model selection blocked a durable observation"
    finally:
        release.set()
        worker.join(5)
        observer.join(5)


def test_execution_failure_is_not_an_unconfirmed_delivery(service):
    runtime, client, root = service
    config = create(client, root)
    run = send(client, config)["run_id"]
    runtime.ctx.durable.fail_task(run, "model unavailable", state=dict(runtime.ctx.durable.task(run).state))
    item = runtime.ctx.input_queue.list(config["conversation_id"])[0]
    assert item["state"] == "completed"
    assert item["terminal"]["stop_reason"] == "error"
    assert item["terminal"]["summary"] == "model unavailable"


def test_drain_cannot_cross_dispatch_reservation(service, monkeypatch):
    from codey.app.task_submit import dispatch_durable_task

    runtime, client, root = service
    config = create(client, root)
    run = send(client, config)["run_id"]
    entered, release, drained = threading.Event(), threading.Event(), threading.Event()
    original = runtime.control.permits_dispatch

    def paused_check(task):
        allowed = original(task)
        entered.set()
        release.wait(3)
        return allowed

    monkeypatch.setattr(runtime.control, "permits_dispatch", paused_check)
    monkeypatch.setattr("codey.app.task_submit.submit_browser_task", lambda *_args, **_kwargs: True)
    worker = threading.Thread(target=lambda: dispatch_durable_task(runtime.ctx.durable.task(run), state=runtime.ctx))
    worker.start()
    assert entered.wait(2)
    operator = ControlClient(runtime.home, operator=True)
    observer = threading.Thread(target=lambda: (operator.lifecycle("drain"), drained.set()))
    observer.start()
    try:
        assert not drained.wait(0.5), "drain crossed a checked but unreserved dispatch"
    finally:
        release.set()
        worker.join(5)
        observer.join(5)
        runtime.ctx.release_run(run)


def test_steering_observes_the_target_execution_instead_of_the_consumption_task(service):
    runtime, client, root = service
    config = create(client, root)
    first = send(client, config)
    target = first["run_id"]
    runtime.ctx.reserve_run(session_id=config["conversation_id"], project=str(root), task="work",
                            provider_id="deepseek", run_id=target)
    try:
        steered = client.call("send_message", {"conversation_id": config["conversation_id"], "request_id": "steer",
                                "control_generation": 1, "text": "Keep it concise", "mode": "steer", "expected_run_id": target})
        assert steered["run_id"] == target
        assert steered["input_run_id"] != target
    finally:
        runtime.ctx.release_run(target)


def test_rpc_is_a_shared_service_client_with_unicode_framing(service) -> None:
    runtime, _, root = service
    records = [
        {"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {"protocol_version": 1}},
        {"jsonrpc": "2.0", "id": "create", "method": "control.create_conversation",
         "params": {"request_id": "rpc-create", "project": str(root), "title": "a\u2028b\u2029c"}},
    ]
    process = subprocess.run([sys.executable, "-m", "codey", "rpc", "--state-home", str(runtime.home)],
                             input="\n".join(json.dumps(item, ensure_ascii=False) for item in records).encode() + b"\n",
                             capture_output=True, timeout=20)
    assert process.returncode == 0, process.stderr.decode(errors="replace")
    responses = [json.loads(line) for line in process.stdout.split(b"\n") if line]
    created = next(item["result"] for item in responses if item["id"] == "create")
    assert created["title"] == "a\u2028b\u2029c"
    assert created["conversation_id"] in {item["id"] for item in runtime.ctx.load_ui_state()["sessions"]}


def test_mcp_advertises_the_common_schema_and_calls_the_same_service(service) -> None:
    pytest.importorskip("mcp")
    from mcp import Client

    from codey.control.mcp import build_server
    from codey.control.protocol import COMMANDS

    _, client, root = service

    async def exercise():
        async with Client(build_server(client)) as mcp:
            tools = await mcp.list_tools()
            assert {item.name for item in tools.tools} == set(COMMANDS)
            result = await mcp.call_tool("create_conversation", {"request_id": "mcp-create", "project": str(root)})
            assert not result.is_error
            return result.structured_content

    created = asyncio.run(exercise())
    assert client.call("get_control", {"conversation_id": created["conversation_id"]}) == created


def test_mcp_stdio_process_attaches_to_the_running_service(service):
    pytest.importorskip("mcp")
    from mcp import Client, StdioServerParameters

    runtime, client, _ = service
    async def exercise():
        parameters = StdioServerParameters(command=sys.executable,
            args=["-m", "codey", "mcp", "--state-home", str(runtime.home)], cwd=Path.cwd())
        async with Client(parameters) as mcp:
            result = await mcp.call_tool("get_health", {})
            assert not result.is_error
            return result.structured_content
    assert asyncio.run(exercise())["instance_id"] == client.call("get_health")["instance_id"]
