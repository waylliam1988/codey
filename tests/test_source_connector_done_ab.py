from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from tests.manual import source_connector_done_ab


class _FakeProvider:
    name = "fake"
    location = "fixture://fake"

    def close(self) -> None:
        pass


def test_trace_bounds_are_owned_by_done_harness() -> None:
    assert isinstance(source_connector_done_ab.MAX_TRACE_BYTES, int)
    assert source_connector_done_ab.MAX_TRACE_BYTES > source_connector_done_ab.MAX_RESULT_BYTES
    assert isinstance(source_connector_done_ab.TRACE_PROMPT_CHARS, int)
    assert isinstance(source_connector_done_ab.TRACE_REPLY_CHARS, int)


def test_source_connector_done_self_test() -> None:
    source_connector_done_ab._self_test()


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_source_connector_done_rerun_failed_replaces_matching_sample_only(tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    _write_json(
        output,
        {
            "probe": "source_connector_done_ab",
            "provider": "deepseek",
            "cases": ["arxiv"],
            "arms": ["batch"],
            "samples": 2,
            "complete": True,
            "rows": [
                {
                    "provider": "deepseek",
                    "case": "arxiv",
                    "arm": "batch",
                    "sample": 1,
                    "error": "TimeoutError: old failure",
                },
                {
                    "provider": "deepseek",
                    "case": "arxiv",
                    "arm": "batch",
                    "sample": 2,
                    "ok": True,
                    "score": 4,
                },
            ],
            "summary": {},
        },
    )

    def fake_run_case(_provider, *, provider_id: str, case, arm: str, sample: int, **_kwargs):
        return {
            "provider": provider_id,
            "case": case.name,
            "arm": arm,
            "sample": sample,
            "ok": True,
            "score": 9,
            "stop_reason": "done",
            "done_attempts": 1,
            "quality_retry_count": 0,
        }

    with (
        mock.patch("tests.manual.source_connector_done_ab.connect_provider", return_value=_FakeProvider()),
        mock.patch("tests.manual.source_connector_done_ab.run_case", side_effect=fake_run_case),
    ):
        source_connector_done_ab.run_provider(
            "deepseek",
            cases=(source_connector_done_ab.CASES["arxiv"],),
            arms=("batch",),
            samples=2,
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
    rows = sorted(payload["rows"], key=lambda row: int(row.get("sample") or 0))
    assert len(rows) == 2
    assert rows[0]["sample"] == 1
    assert rows[0]["ok"] is True
    assert "error" not in rows[0]
    assert rows[1]["sample"] == 2
    assert rows[1]["score"] == 4


def test_source_connector_done_provider_connect_failure_preserves_existing_result(tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    old_payload = {
        "probe": "source_connector_done_ab",
        "provider": "deepseek",
        "cases": ["arxiv"],
        "arms": ["batch"],
        "samples": 1,
        "complete": True,
        "rows": [
            {
                "provider": "deepseek",
                "case": "arxiv",
                "arm": "batch",
                "sample": 1,
                "error": "TimeoutError: old failure",
            }
        ],
        "summary": {"old": True},
    }
    _write_json(output, old_payload)
    before = output.read_text(encoding="utf-8")

    with mock.patch("tests.manual.source_connector_done_ab.connect_provider", side_effect=RuntimeError("no tab")):
        with pytest.raises(RuntimeError, match="no tab"):
            source_connector_done_ab.run_provider(
                "deepseek",
                cases=(source_connector_done_ab.CASES["arxiv"],),
                arms=("batch",),
                samples=1,
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
