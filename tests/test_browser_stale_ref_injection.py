"""The live fixture exercises a real stale reference without asking the model to err."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.types.json_value import JsonObject, decode_json_object, parse_json_object
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.browser_production_live_gate import run_case
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_stale_ref_injection_preserves_the_original_proposal_and_requires_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "run"

    def ref(name: str) -> str:
        snapshots = [row.result for row in tool_receipts(directory / "state")
                     if row.result.ok and row.result.call.name == "browser_snapshot"]
        elements = decode_json_object(snapshots[-1].canonical["value"])["elements"]
        assert isinstance(elements, list)
        return next(str(row["ref"]) for row in elements if isinstance(row, dict) and row.get("name") == name)

    def replies() -> Iterator[JsonObject]:
        for _ in range(2):
            yield {"tool": "browser_snapshot", "args": {}}
            yield {"tool": "browser_fill", "args": {"ref": ref("Locality"), "value": "福州"}}
        yield {"tool": "browser_click", "args": {"ref": ref("Load record"), "purpose": "result"}}
        yield {"tool": "done", "args": {"summary": "净重173克，校验码QUARTZ_851。"}}

    install_http(monkeypatch, replies())
    result = run_case(directory, "chat", Target("http://scripted.test/v1", "fixture", 123456789, 10),
                      inject_stale_ref=True, max_turns=6)
    assert result["passed"] is True and result["failed_stale_refs"] == 1 and result["stale_ref_injected"] is True, result
    injection = parse_json_object((directory / "stale-ref-injection.json").read_text(encoding="utf-8"))
    fills = [row.result for row in tool_receipts(directory / "state") if row.result.call.name == "browser_fill"]
    assert len(fills) == 2 and fills[0].call.args["ref"] == injection["submitted_ref"]
    assert not fills[0].ok and fills[1].ok
