"""Verify receipt-bound writes with real Chromium, version conflicts and scripted proposals."""
from pathlib import Path

import pytest

from tests.test_browser_entry_candidates import action, browser_code, done
from tests.test_browser_outcome_ab import NAVIGATE, READ, WAIT
from tests.test_protocol_surface import TARGET
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import browser_receipt_edit_ab as probe
from tools.completion_artifact_audit import tool_receipts

EDIT = "const read=await tools.read_file({path:'result.json'});return unwrap(await tools.edit({read,changes:CHANGES}));"


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize(("arm", "variant", "defect", "passed"), [
    ("A", "original", "", False),
    ("B", "original", "", True),
    ("B", "alternate", "", True),
    ("B", "alternate", "reload", False),
    ("B", "alternate", "failed_parent", False),
    ("B", "alternate", "model_values", False),
])
def test_verified_browser_fields_are_bound_before_the_real_version_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str, variant: str, defect: str, passed: bool,
) -> None:
    code = browser_code().replace(WAIT, "")
    assert "browser_wait" not in code
    if defect == "reload":
        code = code.replace(READ, f"const body=unwrap(await tools.browser_read({{}}));{NAVIGATE}return body;")
    elif defect == "failed_parent":
        code = code.replace(READ, "unwrap(await tools.browser_read({}));throw Error('return failed');")
    edit = action(EDIT.replace("CHANGES", "{amount:'unknown',code:'unknown'}"
                               if arm == "A" or defect == "model_values" else "{}"))
    amount, token = probe.VARIANTS[variant]
    install_http(monkeypatch, [action(code), edit, edit, done(f"净重 {amount} 克，校验码 {token}。")])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, variant, arm, TARGET, max_turns=4)
    assert row["passed"] is passed, row
    assert row["binding_verified"] is passed
    receipts = tool_receipts(directory / "state")
    edits = [item.result for item in receipts if item.result.call.name == "edit"]
    if defect:
        assert not edits
        assert isinstance(rejections := row["bindings"], list) and len(rejections) == 2
        assert all(isinstance(item, dict) and item["status"] == "rejected" for item in rejections)
        if defect == "failed_parent":
            assert any(item.result.call.name == "browser_read" and item.result.ok for item in receipts)
            assert not any(item.result.call.name == "codemode" and item.result.ok for item in receipts)
    else:
        assert row["true_stale_conflicts"] == 1
        assert row["fresh_read_recovery"] is True
        assert [item.ok for item in edits] == [False, True]
        disk = row["project_result"]
        assert isinstance(disk, dict) and "external_revision" in disk
        assert (disk["amount"], disk["code"]) == ((amount, token) if passed else ("unknown", "unknown"))
