"""Live gates must admit real API connections, not pretend Zen is Local/web."""
from dataclasses import asdict

import pytest

from codey.runtime.core.api_selection import ApiRunSelection
from tools import local_model_gate_attempts as attempts
from tools import local_model_release_gate as release
from tools import local_model_ui_gate as ui


def selection():
    return ApiRunSelection("zen", "test-agreement", "muse-fixture", "openai-responses", True,
                           stream=True, tool_choice="auto", output_tokens=4096)


def test_release_cli_admits_explicit_zen_model_without_local_probe(monkeypatch, tmp_path):
    from codey.providers import api_connections

    monkeypatch.setattr(api_connections, "capture_selection", lambda provider, choice: selection())
    monkeypatch.setattr(release, "probe_endpoint", lambda: (_ for _ in ()).throw(AssertionError("Local probe")))
    monkeypatch.setattr(release, "_metadata", lambda target: {"target": asdict(target)})
    captured = []

    def run(cases, directory, target, **kwargs):
        captured.append(target)
        return [{"case": "chat", "attempt": 1, "ok": True}]

    monkeypatch.setattr(attempts, "run_attempts", run)
    assert release.main(["--provider", "zen", "--model", "muse-fixture", "--case", "chat",
                         "--run-dir", str(tmp_path / "release"), "--json"]) == 0
    assert captured[0].provider_id == "zen"
    assert captured[0].api_selection == selection().to_payload()


def test_release_cli_freezes_explicit_turn_budget_for_live_model_attempts(monkeypatch, tmp_path):
    from codey.providers import api_connections

    monkeypatch.setattr(api_connections, "capture_selection", lambda provider, choice: selection())
    monkeypatch.setattr(release, "_metadata", lambda target: {"turn_budget": target.turn_budget})
    targets = []

    def run(cases, directory, target, **kwargs):
        targets.append(target)
        return [{"case": "edit", "attempt": 1, "ok": True}]

    monkeypatch.setattr(attempts, "run_attempts", run)
    assert release.main(["--provider", "zen", "--model", "muse-fixture", "--case", "edit",
                         "--turn-budget", "24", "--run-dir", str(tmp_path / "release"), "--json"]) == 0
    assert targets[0].turn_budget == 24


def test_ui_zen_preflight_uses_catalog_instead_of_browser_tab(monkeypatch):
    from codey.providers import api_connections

    monkeypatch.setattr(api_connections, "capture_selection", lambda provider, choice: selection())
    monkeypatch.setattr(ui.provider_services, "provider_tab_availability",
                        lambda: (_ for _ in ()).throw(AssertionError("Browser probe")))
    monkeypatch.setenv("UI_GATE_MODEL", "muse-fixture")
    assert ui._preflight("zen") == ("https://opencode.ai/zen/v1", "muse-fixture")


def test_zen_gate_factory_opens_frozen_connection_without_local_identity(monkeypatch, tmp_path):
    from codey.providers import api_connections
    from codey.providers.api_provider import ApiProvider
    from codey.providers.zen.connection import ZenProvider

    runtime = ApiProvider("http://fixture.test/v1", "muse-fixture", api_protocol="openai-responses", stream=True)
    zen = ZenProvider(runtime)
    seen = []

    def connect(admitted):
        seen.append(admitted)
        return zen

    monkeypatch.setattr(api_connections, "open_selection", connect)
    target = attempts.GateTarget("https://opencode.ai/zen/v1", "muse-fixture", 32768, 8192, 12000,
                                 provider_id="zen", api_selection=selection().to_payload())
    with attempts.record_api_generations(tmp_path / "provider.jsonl"):
        provider = attempts.make_provider(target, tmp_path)
    assert provider is zen
    assert seen == [selection()]
    assert provider.runtime.api_protocol == "openai-responses"
    assert provider.runtime.stream is True


def test_responses_gate_history_counts_only_new_results_and_real_output(tmp_path):
    import json

    rows = [
        {"type": "response", "payload": {"output": [{"type": "function_call", "name": "done"}]}},
        {"type": "request", "payload": {"input": [
            {"type": "function_call_output", "output": "ERROR: Not done yet"},
        ]}},
        {"type": "response", "payload": {"output": [{"type": "function_call", "name": "done"}]}},
    ]
    path = tmp_path / "history.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf8")
    result = ui.analyze_provider_history(path)
    assert result["done_calls"] == 2
    assert result["completion_rejections"] == 1


def test_gate_secondary_model_scope_excludes_personal_local_or_web_targets():
    from codey.app import provider_services

    target = attempts.GateTarget("https://opencode.ai/zen/v1", "muse-fixture", 32768, 8192, 12000,
                                 provider_id="zen", api_selection=selection().to_payload())
    with attempts.isolate_gate_secondary_models(target):
        assert provider_services.provider_availability(None) == {"zen": True}


def test_api_gate_observer_counts_real_http_attempt_without_saving_headers(monkeypatch, tmp_path):
    import json

    from codey.providers import api_transport

    def generate(url, payload, headers, **kwargs):
        kwargs["observe"](attempt=1, data=json.dumps(payload).encode(), phase="request", response_bytes=0, seconds=0)
        return {"status": "completed", "output": []}

    monkeypatch.setattr(api_transport, "generate", generate)
    path = tmp_path / "provider.jsonl"
    with attempts.record_api_generations(path):
        api_transport.generate("http://fixture/responses", {"model": "fixture"}, {"Authorization": "secret-fixture"})
    records = [json.loads(line) for line in path.read_text(encoding="utf8").splitlines()]
    assert sum(row["type"] == "wire_attempt" and row["phase"] == "request" for row in records) == 1
    assert "secret-fixture" not in path.read_text(encoding="utf8")


def test_responses_metrics_report_finish_and_actual_budget_not_chat_assumptions(tmp_path):
    import json

    rows = [
        {"type": "request", "exchange": 1, "payload": {"model": "fixture", "input": [], "max_output_tokens": 4096}},
        {"type": "response", "exchange": 1, "payload": {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}},
        {"type": "request", "exchange": 2, "terminal": True,
         "payload": {"model": "fixture", "input": [], "tools": [{"name": "done"}], "max_output_tokens": 4096}},
        {"type": "response", "exchange": 2, "payload": {"status": "completed"}},
    ]
    (tmp_path / "provider.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf8")
    metrics = attempts._provider_metrics(tmp_path)
    assert metrics["active_finish_reasons"] == ["max_output_tokens"]
    assert metrics["terminal_finish_reasons"] == ["completed"]
    assert metrics["output_budget"] == {"active": [4096], "terminal": [4096]}
    assert attempts.annotate_result({"case": "edit", "ok": False, "provider_metrics": metrics})["failure_kind"] == "truncation"


def test_local_responses_recording_observes_real_responses_payload(monkeypatch, tmp_path):
    import json

    from codey.providers.api_provider import ApiProvider
    from codey.providers.base import AssistantTurn, ProviderToolDefinition

    def post(self, pending, tools, timeout=None, **kwargs):
        self._observe_http_attempt(attempt=1, data=b'{"model":"fixture","input":[]}',
                                   phase="request", response_bytes=0, seconds=0)
        return AssistantTurn(raw={"status": "completed", "output": []})

    monkeypatch.setattr(ApiProvider, "_responses_exchange", post)
    target = attempts.GateTarget("http://fixture/v1", "fixture", 32768, 8192, 12000,
                                 api_protocol="openai-responses")
    provider = attempts.RecordingProvider(target, tmp_path)
    provider._responses_exchange([], [ProviderToolDefinition("done", "Finish", {})], None)
    rows = [json.loads(line) for line in (tmp_path / "provider.jsonl").read_text(encoding="utf8").splitlines()]
    assert provider.api_protocol == "openai-responses"
    assert [(r["type"], r.get("exchange")) for r in rows] == [("request", 1), ("wire_attempt", 1), ("response", 1)]


@pytest.mark.parametrize("connection,protocol", [("local", "openai-responses"), ("zen", "openai-responses"),
                                                 ("local", "openai-completions")])
def test_ui_history_records_both_api_protocols_for_local_and_zen(monkeypatch, tmp_path, connection, protocol):
    import json

    from codey.providers import api_transport
    from codey.providers.api_provider import ApiProvider
    from tests.test_api_generation_observations_no_replay import Response

    body = ({"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": "OK"}]}]} if protocol == "openai-responses" else
        {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}]})
    monkeypatch.setattr(api_transport, "open_request", lambda *args: Response(json.dumps(body).encode()))
    path = tmp_path / "history.jsonl"
    provider = ApiProvider("http://fixture/v1", "fixture", api_protocol=protocol)
    with ui._provider_history_recorder(connection, path):
        assert provider.send("fixture") == "OK"
    rows = [json.loads(line) for line in path.read_text(encoding="utf8").splitlines()]
    assert [r["type"] for r in rows] == ["request", "wire_attempt", "wire_attempt", "response"]
    assert ("input" if protocol == "openai-responses" else "messages") in rows[0]["payload"]
