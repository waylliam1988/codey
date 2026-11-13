"""Fixed original failures and boundary checks for task-selected navigation."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from codey.codemode.runtime import NodeWorkerRuntime
from codey.codemode.types import ToolCatalogEntry, ToolCatalogSnapshot
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object
from tests.test_task_contract_fixed_replies import TARGET, install_original_http, saved_replies
from tools.basic_navigation_url_ab import TaskDestination, navigation_surface, run_arm, selector_catalog
from tools.browser_production_live_gate import URL
from tools.completion_artifact_audit import tool_receipts

pytestmark = pytest.mark.usefixtures(
    "public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")


@pytest.mark.parametrize("task", ["查看结果", "不要打开 https://example.com/a", "看看代码 `open https://example.com/a`",
    "打开 https://example.com/a 和 https://example.com/b", "打开 https://127.0.0.1/a",
    "打开 <https://example.com/a#one> 和 <https://example.com/a#two>"])
def test_no_implicit_binding_without_a_unique_authorized_original_url(task: str) -> None:
    with pytest.raises(ValueError):
        TaskDestination.from_task(task)


@pytest.mark.parametrize("url", [URL, "https://EXAMPLE.COM/A-b_C?x=A%2FB&n=0042#Part-1"])
def test_selector_preserves_original_url_and_does_not_navigate_until_called(tmp_path: Path, url: str) -> None:
    destination = TaskDestination.from_task(f"打开 <{url}>，读取页面。")
    assert destination.url == url
    calls: list[JsonObject] = []

    def invoke(name: str, args: dict[str, object]) -> object:
        calls.append({"name": name, "args": decode_json_object(args)})
        return {"ok": True, "value": "Opened"}

    catalog = [ToolCatalogEntry("browser_navigate", "navigate", output_schema={"type": "string"})]
    with navigation_surface(tmp_path, destination, enabled=True):
        runtime = NodeWorkerRuntime()
        before = runtime.execute("return 'not yet';", catalog=catalog, invoke=invoke)
        assert before.ok and not calls
        result = runtime.execute("return unwrap(await tools.browser_navigate({target:'task'}));", catalog=catalog, invoke=invoke)
    assert result.ok and result.value == "Opened"
    assert calls == [{"name": "browser_navigate", "args": {"url": url}}]


@pytest.mark.parametrize("args", ["{}", "{url:'https://example.com/wrong'}", "{target:'other'}", "{target:'task',url:'wrong'}", "null"])
def test_selector_rejects_copied_urls_and_other_targets(tmp_path: Path, args: str) -> None:
    calls: list[str] = []

    def invoke(name: str, values: dict[str, object]) -> object:
        calls.append(name)
        return {"ok": True, "value": "Opened"}

    with navigation_surface(tmp_path, TaskDestination.from_task("打开 " + URL), enabled=True):
        result = NodeWorkerRuntime().execute("return await tools.browser_navigate(" + args + ");",
            catalog=[ToolCatalogEntry("browser_navigate", "navigate", output_schema={"type": "string"})], invoke=invoke)
    assert not result.ok and "target:'task'" in result.error and not calls


def test_selector_changes_only_the_navigation_declaration() -> None:
    navigation = ToolCatalogEntry("browser_navigate", "navigate", parameters={"url": {"type": "string"}})
    snapshot = ToolCatalogEntry("browser_snapshot", "snapshot")
    original = ToolCatalogSnapshot((navigation, snapshot), "host-contract")
    visible = selector_catalog(original)
    assert visible.entries[1] == snapshot
    assert visible.entries[0].output_schema == navigation.output_schema
    assert visible.entries[0].grant == navigation.grant
    assert visible.entries[0].parameters != navigation.parameters
    assert original.entries[0] == navigation


@pytest.mark.parametrize("arm", ["A", "B"])
def test_saved_basic_replies_change_only_navigation_argument_and_reach_real_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str,
) -> None:
    # First compile failure and first attempted repair, followed by a controlled
    # done. Repeating the same completed round three times triggers no_progress.
    original = saved_replies("basic")[:2]
    replies: list[str] = []
    adaptations: list[JsonObject] = []
    for raw in original:
        reply = parse_json_object(raw)
        if arm == "B":
            args = decode_json_object(reply["args"])
            code = args["code"]
            assert isinstance(code, str)
            pattern = r"tools\.browser_navigate\(\{url: 'https://example\.com/production[-_]browser[-_]record'\}\)"
            replaced, count = re.subn(pattern, "tools.browser_navigate({target:'task'})", code)
            assert count == 1
            args["code"] = replaced
            reply["args"] = args
            changed = json.dumps(reply, ensure_ascii=False)
            adaptations.append({"original": raw, "adapted": changed})
            replies.append(changed)
        else:
            replies.append(raw)
    done: JsonObject = {"tool": "done", "args": {"summary": "净重 173 克，校验码 QUARTZ_851。"}}
    replies.append(json.dumps(done, ensure_ascii=False))
    install_original_http(monkeypatch, replies)
    directory = tmp_path / "run"
    row = run_arm(directory, "A" if arm == "A" else "B", TARGET, max_turns=3)
    (directory / "controlled-adaptations.json").write_text(json.dumps(adaptations, ensure_ascii=False), encoding="utf-8")
    assert row["passed"] is (arm == "B"), row
    audit = decode_json_object(row["navigation"])
    assert audit["all_navigations_exact"] is (arm == "B")
    assert audit["browser_chain_reached"] is (arm == "B")
    navigation = audit["formal_navigations"]
    assert isinstance(navigation, list) and len(navigation) == 1
    assert all(decode_json_object(item)["http_status"] == (200 if arm == "B" else 404) for item in navigation)
    assert decode_json_object(navigation[0])["observed_url"] == (URL if arm == "B" else URL.replace("-", "_"))
    parents = [r.result for r in tool_receipts(directory / "state") if r.result.call.name == "codemode"]
    assert parents[0].audit["error_code"] == "syntax_error"
    assert all("recovery did not consume" not in parent.model_text for parent in parents)
