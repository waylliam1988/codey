"""Inject observed model mistakes through the production entry and Chromium."""
from pathlib import Path

import pytest

from tests.test_browser_entry_candidates import action, browser_code, done
from tests.test_protocol_surface import TARGET
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import browser_three_fixes_ab as probe
from tools.browser_production_live_gate import URL
from tools.completion_artifact_audit import tool_receipts


@pytest.mark.parametrize("source", [
    "window.token", "return window.token();", "return\nwindow.token;", "return window.token; alert(1)",
    "() => { return window.token; }", "return window['token'];",
])
def test_property_repair_leaves_other_code_unchanged(source: str) -> None:
    assert probe.property_expression(source) == source


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("arm", ["A", "B"])
@pytest.mark.parametrize("scenario", probe.SCENARIOS)
def test_observed_failure_and_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                        arm: str, scenario: str) -> None:
    code = browser_code()
    expected = "净重 173 克，校验码 QUARTZ_851。"
    if scenario == "legacy_structured":
        script = code.replace("return unwrap(await tools.browser_read({}));",
            "const body=unwrap(await tools.browser_read({}));"
            "const url=unwrap(await tools.browser_tab_list({})).page_url;"
            "const token=unwrap(await tools.browser_eval({code:'return window.__contractToken;'})).value;"
            "return {body,url,token};")
        assert script != code
        replies = [action(script), *[done(f"{expected} URL {URL}，token SCHEMA_TOKEN_74。")] * 3]
    elif scenario == "reread":
        nav = "unwrap(await tools.browser_navigate({url:inputs.url}));"
        script = code.replace(nav, nav + "unwrap(await tools.browser_read({}));")
        assert script != code
        replies = [action(script), done("净重 173 克，校验码 QUARTZ_85。")]
    else:
        args = "{read:r,content}" if arm == "B" else "{path:'result.json',content}"
        attempt = action("const r=await tools.read_file({path:'result.json'});"
            "const content=JSON.stringify({...JSON.parse(unwrap(r).text),amount:173,code:'QUARTZ_851'});"
            f"return unwrap(await tools.edit({args}));")
        replies = [action(code), attempt, attempt, done(expected)]
    install_http(monkeypatch, replies)
    directory = tmp_path / "run"
    row = probe.run_arm(directory, scenario, arm, TARGET, max_turns=len(replies))
    assert row["passed"] is (arm == "B"), row
    if scenario == "legacy_structured":
        evaluation, = [r.result for r in tool_receipts(directory / "state") if r.result.call.name == "browser_eval"]
        assert evaluation.ok is (arm == "B")
        assert evaluation.call.args["code"] == "return window.__contractToken;"
        if arm == "B":
            assert row["candidate_actions"] == [{"original": "return window.__contractToken;",
                                                "executed": "window.__contractToken", "ok": True}]
    elif scenario == "reread":
        assert row["source_reads"] == 2
        if arm == "B":
            actions = row["candidate_actions"]
            assert isinstance(actions, list) and len(actions) == 1
            assert isinstance(actions[0], dict) and actions[0]["model_text"] == "净重 173 克，校验码 QUARTZ_85。"
            assert row["answer"] == "加载后的页面记录显示净重 173 克，校验码 QUARTZ_851。"
    else:
        assert row["true_stale_conflicts"] == (1 if arm == "B" else 0)
        assert row["fresh_read_recovery"] is (arm == "B")


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_reread_does_not_publish_facts_from_replaced_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    nav = "unwrap(await tools.browser_navigate({url:inputs.url}));"
    script = browser_code().replace(nav, nav + "unwrap(await tools.browser_read({}));")
    script = script.replace("return unwrap(await tools.browser_read({}));",
                            "const body=unwrap(await tools.browser_read({}));" + nav + "return body;")
    assert script.count(nav) == 2
    install_http(monkeypatch, [action(script), done("已完成。")])
    row = probe.run_arm(tmp_path / "run", "reread", "B", TARGET, max_turns=2)
    assert row["stop_reason"] == "done" and row["source_reads"] == 2
    assert row["candidate_actions"] == []
    assert row["passed"] is False
