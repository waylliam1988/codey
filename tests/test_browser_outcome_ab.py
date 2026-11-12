"""Verify the two experimental candidates with real Chromium and scripted proposals."""
from pathlib import Path

import pytest

from tests.test_browser_entry_candidates import action, browser_code, done
from tests.test_protocol_surface import TARGET
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import browser_outcome_ab as probe
from tools.browser_production_live_gate import URL

FACTS = "净重 173 克，校验码 QUARTZ_851。"
READ = "return unwrap(await tools.browser_read({}));"
WAIT = "unwrap(await tools.browser_wait({condition:'text',value:'鱼丸',timeout_ms:5000}));"
NAVIGATE = "unwrap(await tools.browser_navigate({url:inputs.url}));"
EDIT = ("const read=await tools.read_file({path:'result.json'});"
        "return unwrap(await tools.edit({read,changes:{amount:173,code:'QUARTZ_851'}}));")


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize(("arm", "token", "passed"), [("A", True, True), ("B", True, True), ("B", False, False)])
def test_text_delivery_uses_original_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str, token: bool, passed: bool,
) -> None:
    code = browser_code().replace(READ, "const body=unwrap(await tools.browser_read({}));"
        "const url=unwrap(await tools.browser_tab_list({})).page_url;"
        + ("const token=unwrap(await tools.browser_eval({code:'window.__contractToken'})).value;" if token else "")
        + ("return {body,url,token};" if token else "return {body,url};"))
    summary = (f"{FACTS} URL {URL.replace('production-browser-record', 'production_browser_record')}，"
               "token SCHEMA_TOKEN_74。") if arm == "A" else FACTS
    install_http(monkeypatch, [action(code), done(summary)])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, "legacy_structured", arm, TARGET, max_turns=2)
    assert row["passed"] is passed, row
    if passed and arm == "A":
        assert "production_browser_record" not in str(row["answer"])
        assert f"当前标签页 URL：{URL}" in str(row["answer"])
        assert not (directory / "rendered-answer.txt").exists()
    elif passed:
        assert row["rendered_answer"] == f"{FACTS}\n\n页面地址：{URL}\nToken：SCHEMA_TOKEN_74\n"
        assert row["raw_explanation_preserved"] is True
    else:
        assert not (directory / "rendered-answer.txt").exists()


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize(("case", "arm", "reload", "passed"), [
    ("stale_edit", "A", False, False), ("stale_edit", "B", False, True),
    ("stale_edit_delayed", "B", False, True), ("stale_edit", "B", True, False),
])
def test_post_load_read_preserves_real_conflict_and_rejects_replaced_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, arm: str, reload: bool, passed: bool,
) -> None:
    code = browser_code().replace(WAIT, "")
    assert "browser_wait" not in code
    if reload:
        code = code.replace(READ, NAVIGATE + READ)
    install_http(monkeypatch, [action(code), action(EDIT), action(EDIT), done(FACTS)])
    row = probe.run_arm(tmp_path / "run", case, arm, TARGET, max_turns=4)
    assert row["passed"] is passed, row
    assert row["explicit_waits"] == 0
    assert row["true_stale_conflicts"] == 1
    assert row["fresh_read_recovery"] is True
    if arm == "B":
        evidence = row["evidence"]
        assert isinstance(evidence, list) and evidence
        assert isinstance(check := evidence[-1], dict) and check["passed"] is passed
        assert isinstance(refs := check["refs"], list) and len(refs) == (2 if passed else 0)
        if case == "stale_edit_delayed":
            readiness = row["readiness"]
            assert isinstance(readiness, list) and len(readiness) == 1
            assert isinstance(item := readiness[0], dict) and item["matched"] is True
            elapsed = item["seconds"]
            assert isinstance(elapsed, (int, float)) and elapsed > 0.3
