"""Natural Research handoff through Chromium and the real completion gate."""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.types.json_value import JsonObject, decode_json_object
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import research_natural_handoff_ab as probe
from tools.completion_explanation_ab import Target


def test_literal_evidence_guard_rejects_invented_or_stale_quotes() -> None:
    evidence = probe.Evidence("key", {"e2": "order ready at 18:20"})
    for label, text in (("e1", "18:20"), ("e2", "18:30")):
        assert probe.decision(evidence, {"state": "ready", "missing": "", "quotes": [{"label": label, "text": text}]}) == "invalid"
    assert probe.decision(evidence, {"state": "missing", "missing": "location", "quotes": []}) == "missing"
    assert probe.decision(evidence, {"state": "missing", "missing": "location", "quotes": [
        {"label": "e2", "text": "18:20"}]}) == "missing"


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("name", ["visible", "interactive", "missing", "decoy"])
def test_natural_request_preserves_acquisition_until_requested_facts_are_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    case = probe.CASES["missing" if name == "decoy" else name]
    directory = tmp_path / "run"

    def phase() -> JsonObject:
        path = directory / "handoff.jsonl"
        return decode_json_object(json.loads(path.read_text(encoding="utf-8").splitlines()[-1]))

    def code(js: str) -> JsonObject:
        return {"tool": "codemode", "args": {"code": js, "inputs": {"url": case.url}}}

    def replies() -> Iterator[JsonObject]:
        if phase()["phase"] == "collect":
            yield code("unwrap(await tools.browser_navigate({url:inputs.url})); return unwrap(await tools.browser_read({}));")
        assert phase()["phase"] == "assess", phase()
        if name not in {"visible", "decoy"}:
            yield {"tool": "codemode", "args": {"coverage": {"state": "missing", "missing": "The requested details", "quotes": []}}}
            state = phase()
            assert state["phase"] == "collect" and isinstance(tools := state["tools"], list)
            assert "codemode" in tools and "submit_research_answer" not in tools
            wait = "unwrap(await tools.browser_wait({condition:'text',value:'C-42',timeout_ms:5000}));" if name == "interactive" else ""
            yield code("const page=unwrap(await tools.browser_snapshot({}));"
                       "unwrap(await tools.browser_click({ref:page.elements.find(e=>e.role==='button').ref}));" + wait
                       + "return unwrap(await tools.browser_read({}));")
        state = phase()
        assert state["phase"] == "assess", state
        label = next(iter(decode_json_object(state["evidence"])))
        facts = ("运输中", "周四 12:10") if name == "decoy" else case.facts
        yield {"tool": "codemode", "args": {"coverage": {"state": "ready", "missing": "",
            "quotes": [{"label": label, "text": fact} for fact in facts]}}}
        state = phase()
        assert state["phase"] == "submit" and state["tools"] == ["submit_research_answer"], state
        yield {"tool": "submit_research_answer", "args": {"state": "answered",
            "markdown": f"页面显示 {'，'.join(facts)}。 [{label}]"}}

    install_http(monkeypatch, replies())
    result = probe.run_arm(directory, case, "B", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10))
    if name == "decoy":
        # Exact quotes can belong to the wrong record. The independent
        # scenario judge must catch that even when production accepts the citation.
        assert result["stop_reason"] == "done" and result["passed"] is False and result["early_submit_only_turns"], result
        return
    assert result["passed"] is True and result["early_submit_only_turns"] == [], result
    assert result["waits"] == (1 if name == "interactive" else 0)
    assert result["assessment_calls"] == (1 if name == "visible" else 2)
