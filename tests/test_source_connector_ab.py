from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from tests.manual import source_connector_ab


class _FakeProvider:
    name = "fake"
    location = "fixture://fake"

    def close(self) -> None:
        pass


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_source_connector_rerun_failed_replaces_old_row(tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    _write_json(
        output,
        {
            "probe": "source_connector_ab",
            "provider": "deepseek",
            "cases": ["pubmed"],
            "arms": ["baseline"],
            "complete": True,
            "rows": [
                {
                    "provider": "deepseek",
                    "case": "pubmed",
                    "arm": "baseline",
                    "error": "TimeoutError: old failure",
                }
            ],
            "summary": {},
        },
    )

    def fake_run_case(_provider, *, provider_id: str, case, arm: str, **_kwargs):
        return {
            "provider": provider_id,
            "case": case.name,
            "arm": arm,
            "ok": True,
            "score": 9,
            "stop_reason": "done",
        }

    with (
        mock.patch("tests.manual.source_connector_ab.connect_provider", return_value=_FakeProvider()),
        mock.patch("tests.manual.source_connector_ab.run_case", side_effect=fake_run_case),
    ):
        source_connector_ab.run_provider(
            "deepseek",
            cases=(source_connector_ab.CASES["pubmed"],),
            arms=("baseline",),
            port=9222,
            output=output,
            max_turns=1,
            send_timeout=1,
            new_chat_timeout=1,
            open_if_missing=False,
            rerun_failed=True,
            trace=None,
            run_id="result",
        )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["ok"] is True
    assert "error" not in payload["rows"][0]


def test_source_connector_provider_connect_failure_preserves_existing_result(tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    old_payload = {
        "probe": "source_connector_ab",
        "provider": "deepseek",
        "cases": ["pubmed"],
        "arms": ["baseline"],
        "complete": True,
        "rows": [
            {
                "provider": "deepseek",
                "case": "pubmed",
                "arm": "baseline",
                "error": "TimeoutError: old failure",
            }
        ],
        "summary": {"old": True},
    }
    _write_json(output, old_payload)
    before = output.read_text(encoding="utf-8")

    with mock.patch("tests.manual.source_connector_ab.connect_provider", side_effect=RuntimeError("no tab")):
        with pytest.raises(RuntimeError, match="no tab"):
            source_connector_ab.run_provider(
                "deepseek",
                cases=(source_connector_ab.CASES["pubmed"],),
                arms=("baseline",),
                port=9222,
                output=output,
                max_turns=1,
                send_timeout=1,
                new_chat_timeout=1,
                open_if_missing=False,
                rerun_failed=True,
                trace=None,
                run_id="result",
            )

    assert output.read_text(encoding="utf-8") == before
