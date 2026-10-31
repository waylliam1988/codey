"""Local diagnostic failures neither change responses nor invent archive facts."""

import io
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from http.client import IncompleteRead
from urllib.error import HTTPError

import pytest

from tests import test_research_deterministic_completion as scenario
from tests.manual import kobold_diagnostics
from tests.manual.research_semantic_review_local import archived_investigations, archived_report, main


@pytest.mark.parametrize("failure", [
    TimeoutError("diagnostic timed out"),
    HTTPError("http://localhost/check", 404, "not found", {}, None),
    IncompleteRead(b"partial"),
    json.JSONDecodeError("invalid diagnostic JSON", "x", 0),
])
def test_optional_native_diagnostic_records_failure_without_raising(monkeypatch, failure):
    def unavailable(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(kobold_diagnostics, "urlopen", unavailable)
    events = []
    kobold_diagnostics.record_native_generation("http://127.0.0.1:5001", events.append)
    assert len(events) == 1
    assert events[0]["event"] == "native_generation_diagnostic_failure"
    assert repr(failure) == events[0]["error"]


def test_native_diagnostic_preserves_the_raw_generation(monkeypatch):
    body = {"results": [{"text": "<tool_call>raw output</tool_call>"}]}
    monkeypatch.setattr(kobold_diagnostics, "urlopen", lambda *_args, **_kwargs: io.BytesIO(json.dumps(body).encode()))
    events = []
    kobold_diagnostics.record_native_generation("http://127.0.0.1:5001/", events.append)
    assert events == [{"event": "native_generation", "body": body}]


@pytest.mark.usefixtures("public_source_dns")
def test_archive_review_restores_real_question_bound_investigations_without_acquisition(tmp_path, monkeypatch):
    source = tmp_path / "archive"
    state = source / "state"
    state.mkdir(parents=True)
    result = scenario._run(state)
    assert result.stop_reason == "done"
    with closing(sqlite3.connect(state / "state.sqlite")) as original, closing(
        sqlite3.connect(state / "durable.sqlite3")
    ) as copy:
        original.backup(copy)
    (source / "research-report.txt").write_text(result.summary, encoding="utf-8")
    (source / "research-control.json").write_text(json.dumps({"question": scenario.TASK}), encoding="utf-8")
    shutil.copytree(state / "knowledge", source / "knowledge")
    report, run_id = archived_report(source)
    assert report == result.accepted_report and run_id == "r"
    before = (state / "calls.jsonl").read_bytes()
    investigations = archived_investigations(source, run_id, tmp_path / "imported-state.sqlite3")
    assert investigations["a1"]["tool"] == "web_search"
    assert investigations["a2"]["tool"] == "open_url"
    assert investigations["a2"]["passage_refs"]
    assert all(row["question_ref"] == "q1" for row in investigations.values())
    directory = tmp_path / "assessment"
    monkeypatch.setattr(sys, "argv", ["research_semantic_review_local", "--source", str(source),
                                      "--directory", str(directory), "--prepare-only"])
    main()
    basis = json.loads((directory / "basis.json").read_text(encoding="utf-8"))
    assert basis["investigations"] == {ref: investigations[ref] for ref in ("a1", "a2")}
    assert basis["report_digest"] == report.to_payload()["compilation_digest"]
    assert archived_report(source)[0] == report
    assert (state / "calls.jsonl").read_bytes() == before
