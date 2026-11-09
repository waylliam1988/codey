"""Review reports distinguish actual delivered policy/branches from configuration."""
from __future__ import annotations

import json
from pathlib import Path

from tools.browser_policy_options import GUIDANCE
from tools.review_browser_experiments import inspect_run


def test_configured_delta_does_not_mean_it_was_delivered(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"projection": "delta", "guidance": "query", "tools": [],
        "original_passages": {"e1": "Background.\nDefault is 46 days.\nLegal hold overrides deletion."}}), encoding="utf-8")
    provider = tmp_path / "provider.jsonl"
    provider.write_text(json.dumps({"type": "request", "payload": {"messages": [
        {"role": "system", "content": GUIDANCE["query"]},
        {"role": "user", "content": "Delta baseline unavailable; full current observation:"},
    ]}}), encoding="utf-8")
    report = inspect_run(summary)
    assert report["delta_delivered"] is False
    assert report["guidance_delivered"] is True
    assert report["browser_contract_exposed"] is False
    provider.write_text(json.dumps({"type": "request", "payload": {"messages": [
        {"role": "system", "content": "- browser_snapshot: current controls"},
        {"role": "user", "content": "Baseline from settled receipt result_1"},
    ]}}), encoding="utf-8")
    report = inspect_run(summary)
    assert report["delta_delivered"] is True
    assert report["browser_contract_exposed"] is True
    assert report["guidance_delivered"] is False
