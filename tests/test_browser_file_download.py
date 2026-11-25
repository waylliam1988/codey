import base64
import hashlib
from pathlib import Path
from typing import NoReturn

import pytest

from codey.automation.browser_contracts import RouteDecision as Route
from codey.automation.browser_routes import route_policy
from codey.codemode.kernel import build_execute_turn_executor
from codey.codemode.runtime import NodeWorkerRuntime
from codey.durable import DurableSession, SQLiteDurableStore
from codey.operations.kernel.kernel_execution import execute_turn
from codey.operations.recovery import record_entry_policy
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.action import MAX_MANAGED_OUTPUT_BYTES, MAX_MANAGED_OUTPUTS_PER_RUN
from codey.runtime.core.models import ToolCall, ToolResult
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject, decode_json_object
from tests.test_browser_production_execution import URL, BrowserFixture
from tests.test_browser_production_execution import production_browser as production_browser
from tests.test_tool_task_background import open_runtime

PAYLOAD = b"\x00\xffdownloaded bytes\n"


def test_real_download_settles_one_durable_artifact_and_replay_never_clicks_again(
    production_browser: BrowserFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, tools, browser = production_browser

    def document(route: Route) -> None:
        data_url = "data:application/octet-stream;base64," + base64.b64encode(PAYLOAD).decode("ascii")
        route.fulfill(content_type="text/html", body=f'<a href="{data_url}" download="fixture.bin">Download fixture</a>')

    monkeypatch.setattr(browser._process, "network_policy", route_policy(document))
    runtime = open_runtime(tmp_path)
    mutations, managed, sink = runtime.app.runtime_mutations, runtime.app.managed_outputs, runtime.sink
    durable, state = runtime.app.durable, runtime.app.state_home
    assert managed is not None and state is not None
    record_entry_policy(mutations, session_id="s", run_id="r", policy=session.policy, durable=durable)
    snapshot = build_turn_snapshot(session)
    parent = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="r", effect_scope="outer", turn=0,
        research_tools=tools, managed_outputs=managed, session_id="s", intent_sink=sink, durable=durable)
    call = ToolCall("codemode", {"code":
        "unwrap(await tools.browser_navigate({url:inputs.url})); "
        "const s=unwrap(await tools.browser_snapshot({})); "
        "return unwrap(await tools.browser_download({ref:s.elements.find(e=>e.name==='Download fixture').ref}));",
        "inputs": {"url": URL}}, call_id="download-parent")
    def execute() -> ToolResult:
        return execute_turn(session, [call], executors={"codemode": parent}, run_id="r", effect_scope="outer", turn=0,
            snapshot=snapshot, intent_sink=sink, durable=durable, managed_outputs=managed,
            session_id="s", research_tools=tools)[0]

    def restarted_vm(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail("download replay restarted VM")

    try:
        result = execute()
        assert result.ok, result.model_text
        value = decode_json_object(result.canonical["value"])
        artifacts = durable.artifacts_for_task("r")
        files = [artifact for artifact in artifacts if artifact.metadata.get("kind") == "browser_download"]
        assert len(files) == 1
        file = files[0]
        assert file.handle == value["handle"] and file.sha256 == hashlib.sha256(PAYLOAD).hexdigest()
        child_ids = result.canonical["child_effect_ids"]
        assert isinstance(child_ids, list) and file.owner_effect_id in child_ids
        path = Path(file.storage_path)
        path.resolve().relative_to(managed.root.resolve())
        browser.close()
        assert path.read_bytes() == PAYLOAD
        monkeypatch.setattr(NodeWorkerRuntime, "execute", restarted_vm)
        replay = execute()
        assert replay.ok and replay.canonical == result.canonical
        assert durable.artifacts_for_task("r") == artifacts
        assert not tools.ledger.read_passages
    finally:
        assert runtime.app.close()
    reopened = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
    try:
        assert reopened.artifacts_for_task("r") == artifacts
        assert path.read_bytes() == PAYLOAD
    finally:
        reopened.close()


def test_binary_artifacts_reject_truncation_corruption_and_cross_scope(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"x" * (MAX_MANAGED_OUTPUT_BYTES + 1))
    store = ManagedOutputStore(tmp_path / "state")
    with pytest.raises(ValueError, match="size or count"):
        store.write_file_artifact(session_id="s", run_id="r", source=source, permission_profile="coding_writer")
    source.write_bytes(PAYLOAD)
    ref = store.write_file_artifact(session_id="s", run_id="r", source=source, permission_profile="coding_writer")
    value: JsonObject = {"handle": ref.handle, "sha256": ref.sha256, "bytes": ref.stored_bytes, "path": str(ref.path)}
    assert store.file_artifact_record(value, session_id="s", run_id="r", owner_effect_id="e").byte_count == len(PAYLOAD)
    with pytest.raises(OSError):
        store.file_artifact_record(value, session_id="other", run_id="r", owner_effect_id="e")
    ref.path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="differs from its receipt"):
        store.file_artifact_record(value, session_id="s", run_id="r", owner_effect_id="e")


def test_text_and_binary_outputs_share_the_existing_run_quota(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(PAYLOAD)
    store = ManagedOutputStore(tmp_path / "state")
    for _ in range(MAX_MANAGED_OUTPUTS_PER_RUN):
        store.write_file_artifact(session_id="s", run_id="r", source=source, permission_profile="coding_writer")
    assert store.write_tool_output(session_id="s", run_id="r", tool_id="t", permission_profile="coding_writer",
                                   tool_name="run", display_ref="test", text="output") is None
    with pytest.raises(ValueError, match="size or count"):
        store.write_file_artifact(session_id="s", run_id="r", source=source, permission_profile="coding_writer")
