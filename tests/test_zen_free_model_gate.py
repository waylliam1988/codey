"""The manual live gate follows current reasoning and connection contracts."""
import io
import json
from contextlib import redirect_stdout

import pytest

from codey.runtime.core.api_selection import ApiRunSelection
from codey.runtime.core.reasoning import compile_selection
from tools import zen_free_model_gate as gate


@pytest.mark.parametrize("model,expected", [
    ({"efforts": ["low", "high", "max"], "thinking_toggle": True},
     [{}, {"effort": "low"}, {"effort": "high"}, {"effort": "max"}]),
    ({"efforts": [], "thinking_toggle": True}, [{}, {"thinking": True}, {"thinking": False}]),
    ({"efforts": [], "thinking_toggle": False}, [{}]),
])
def test_live_reasoning_choices_are_admitted_by_production(model, expected):
    assert gate.reasoning_choices(model) == expected
    for choice in expected:
        compile_selection(efforts=model["efforts"], toggle=model["thinking_toggle"],
                          effort=choice.get("effort"), thinking=choice.get("thinking"))


def selection(model="fixture"):
    return ApiRunSelection("zen", "fixture-revision", model, "openai-completions", True, stream=True)


@pytest.fixture
def catalog(monkeypatch):
    models = [{"id": name, "efforts": ["low", "high"], "thinking_toggle": True} for name in ("one", "two")]
    monkeypatch.setattr(gate.connection, "refresh_models", lambda *, force: {"models": models, "stale": not force})
    monkeypatch.setattr(gate.connection, "capture_selection", lambda choice: selection(choice["model"]))
    monkeypatch.setattr(gate.connection, "endpoint_for_selection", lambda _: "https://fixture.test/v1")
    monkeypatch.setattr(gate, "_metadata", lambda target: {"model": target.model})
    return models


def test_live_matrix_retains_failure_and_continues_requested_models(monkeypatch, tmp_path, catalog):
    calls = []

    def conversation(chosen, case, timeout):
        calls.append((chosen.model_id, case))
        if chosen.model_id == "one" and case == "two_turn_json":
            raise RuntimeError("fixture upstream failure")
        return {"ok": True}

    monkeypatch.setattr(gate, "_conversation", conversation)
    directory = tmp_path / "live"
    assert not gate.run_matrix(directory, 10, models=["one", "two"], cases=["two_turn_json", "two_turn_sse"])
    rows = json.loads((directory / "summary.json").read_text())["results"]
    assert len(calls) == len(rows) == 4
    assert rows[0]["error_class"] == "RuntimeError"
    assert not rows[0]["ok"]
    assert all(row["ok"] for row in rows[1:])


def test_live_matrix_does_not_resend_after_terminal_region_refusal(monkeypatch, tmp_path, catalog):
    class RegionRefusal(RuntimeError):
        status = 403
        error_type = "RegionError"

    calls = []

    def conversation(chosen, case, timeout):
        calls.append(chosen.model_id)
        if chosen.model_id == "one":
            raise RegionRefusal("fixture region restriction")
        return {"ok": True}

    monkeypatch.setattr(gate, "_conversation", conversation)
    directory = tmp_path / "live"
    assert not gate.run_matrix(directory, 10, cases=["two_turn_json", "two_turn_sse"])
    rows = json.loads((directory / "summary.json").read_text())["results"]
    assert calls == ["one", "two", "two"]
    assert rows[1]["not_run"] is True
    assert rows[1]["ok"] is False
    assert rows[2]["ok"] and rows[3]["ok"]


def test_live_matrix_selects_models_cases_and_effort_axis(monkeypatch, tmp_path, catalog):
    choices = []

    def capture(choice):
        choices.append(choice)
        return selection(choice["model"])

    monkeypatch.setattr(gate.connection, "capture_selection", capture)
    monkeypatch.setattr(gate, "_reasoning", lambda chosen, timeout: {"ok": True})
    directory = tmp_path / "live"
    assert gate.run_matrix(directory, 10, models=["two"], cases=["reasoning"])
    rows = json.loads((directory / "summary.json").read_text())["results"]
    assert [row["choice"] for row in rows] == [{}, {"effort": "low"}, {"effort": "high"}]
    assert all(choice["model"] == "two" and "thinking" not in choice for choice in choices)


@pytest.mark.parametrize("stream", [False, True])
def test_live_two_turns_use_one_frozen_production_connection(monkeypatch, stream):
    opened = []
    closed = []
    prompts = []

    class Provider:
        context_ledger = type("Ledger", (), {"view": [1, 2, 3, 4]})()

        def send(self, prompt, *, timeout):
            prompts.append(prompt)
            return "ACK" if len(prompts) == 1 else "COPPER-742"

        def close(self):
            closed.append(True)

    def connect(chosen):
        opened.append(chosen)
        return Provider()

    monkeypatch.setattr(gate.connection, "open_selection", connect)
    admitted = selection()
    row = gate._conversation(admitted, "two_turn_sse" if stream else "two_turn_json", 10)
    assert row["ok"] and row["marker_returned"]
    assert len(opened) == 1 and opened[0].stream is stream
    assert admitted.stream is True
    assert len(prompts) == 2 and closed == [True]


def test_live_worker_receives_actual_frozen_protocol_and_selection(monkeypatch, tmp_path, catalog):
    targets = []

    def worker(case, directory, target, *, timeout):
        targets.append(target)
        return {"case": case, "ok": True}

    monkeypatch.setattr(gate.attempts, "run_case_process", worker)
    assert gate.run_matrix(tmp_path / "live", 10, models=["two"], cases=["edit", "shell_approve"])
    assert len(targets) == 2
    assert all(target.provider_id == "zen" and target.api_protocol == "openai-completions"
               and target.api_selection == selection("two").to_payload() for target in targets)


def test_live_gate_rejects_historical_model_before_sending(monkeypatch, tmp_path, catalog):
    directory = tmp_path / "live"
    with pytest.raises(ValueError, match="current free catalog"):
        gate.run_matrix(directory, 10, models=["deprecated-fixture"])
    assert not directory.exists()


def test_redirected_windows_console_cannot_abort_after_a_unicode_model_reply(monkeypatch, tmp_path, catalog):
    monkeypatch.setattr(gate, "_conversation", lambda *args: {"ok": True, "summary": "100 \u2212 20"})
    buffer = io.BytesIO()
    with io.TextIOWrapper(buffer, encoding="gbk") as output, redirect_stdout(output):
        assert gate.run_matrix(tmp_path / "live", 10, models=["one"], cases=["two_turn_sse"])
        output.flush()
        assert json.loads(buffer.getvalue().decode("gbk"))["summary"] == "100 \u2212 20"
