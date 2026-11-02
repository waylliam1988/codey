"""Constrained compatibility output uses the existing task/provider lifecycle."""

from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.providers import api_context, api_generation, local_config, local_connection, local_selection
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolDefinition
from codey.providers.text_tool_schema import text_tool_schema
from codey.runtime.core.api_selection import ApiRunSelection


def test_json_mode_roundtrips_and_rejects_incompatible_settings():
    previous = local_config.LocalProviderConfig()
    body = {"base_url": "http://fixture/v1", "model": "fixture", "native_tools_mode": "json"}
    config, error = local_config.parse_local_config_update(body, previous)
    assert not error and config.thinking_enabled is False
    assert local_config.config_from_dict(local_config.config_to_payload(config)) == config
    for overrides in ({"thinking_enabled": True}, {"api_protocol": "openai-responses"}):
        assert local_config.parse_local_config_update({**body, **overrides}, previous)[0] is None


def test_frozen_json_selection_survives_changed_connection_defaults(monkeypatch):
    config = local_config.LocalProviderConfig(base_url="http://fixture/v1", model="fixture",
        native_tools_mode="json", thinking_enabled=False, connection_revision="fixture")
    selection = ApiRunSelection("local", local_connection._target_revision(config), "fixture", "openai-completions", False,
                                thinking_enabled=False, text_tool_format="json_schema")
    restored = ApiRunSelection.from_payload(selection.to_payload())
    monkeypatch.setattr(local_config, "load_local_config", lambda: replace(config, native_tools_mode="on", thinking_enabled=True))
    monkeypatch.setattr(local_selection, "load_local_config", lambda: config)
    assert local_selection.capture_local_run_config({"base_url": config.base_url, "model": config.model}).thinking_enabled is False
    provider = local_connection.open_selection(restored)
    try:
        assert provider.native_tools is False and provider.thinking_enabled is False
        assert provider.text_tool_format == "json_schema"
    finally:
        provider.close()
    with pytest.raises(ValueError, match="thinking disabled"):
        replace(selection, thinking_enabled=True)


def test_ordinary_and_auxiliary_requests_are_not_forced_into_tool_envelopes(monkeypatch):
    payloads = []

    def generate(_url, payload, _headers, **_kwargs):
        payloads.append(payload)
        text = '{"tool":"done","args":{"summary":"two"}}' if "response_format" in payload else "ordinary prose"
        return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}

    monkeypatch.setattr(api_generation, "generate", generate)
    provider = ApiProvider("http://fixture/v1", "fixture", native_tools=False, thinking_enabled=False,
                           text_tool_format="json_schema")
    auxiliary = api_context.fork_auxiliary(provider)
    try:
        assert provider.send("hello") == "ordinary prose"
        assert '"done"' in provider.send("task", tool_call_json=True)
        assert auxiliary.send("summarize") == "ordinary prose"
        assert ["response_format" in p for p in payloads] == [False, True, False]
        schema = payloads[1]["response_format"]["json_schema"]["schema"]
        assert schema["properties"] == {"tool": {"type": "string"}, "args": {"type": "object"}}
    finally:
        auxiliary.close()
        provider.close()


@pytest.mark.usefixtures("no_external_advisor_models")
def test_headless_task_admits_constrained_request_through_shared_kernel(tmp_path, monkeypatch):
    payloads = []

    def generate(_url, payload, _headers, **_kwargs):
        payloads.append(payload)
        return {"choices": [{"message": {"content": '{"tool":"done","args":{"summary":"2"}}'}, "finish_reason": "stop"}]}

    monkeypatch.setattr(api_generation, "generate", generate)
    provider = ApiProvider("http://fixture/v1", "fixture", native_tools=False, thinking_enabled=False,
                           text_tool_format="json_schema")
    result = run_headless(HeadlessRequest(project=None, task="What is 1+1?", intent="hybrid", provider_id="qwen",
        session_id="json-task", state_home=tmp_path / "state", research_store_root=tmp_path / "knowledge"),
        connect_provider=lambda *_args, **_kwargs: provider, emit_jsonl=lambda _event: None)
    assert result.exit_code == 0 and result.stop_reason == "done"
    assert payloads and all("response_format" in p for p in payloads)
    schema = payloads[0]["response_format"]["json_schema"]["schema"]
    validator = Draft202012Validator(schema)
    assert validator.is_valid({"tool": "done", "args": {"summary": "2"}})
    assert not validator.is_valid({"tool": "made_up", "args": {}})
    assert not validator.is_valid({"tool": "done", "args": []})
    assert not any("tools" in p for p in payloads)
    assert "Tool contract (use exactly these shapes):" in payloads[0]["messages"][0]["content"]
    assert payloads[0]["messages"][0]["role"] == "system"
    assert all("Tool contract (use exactly these shapes):" not in str(row.get("content", ""))
               for row in payloads[0]["messages"][1:])


def test_frozen_schema_leaves_argument_order_and_validation_to_kernel():
    tools = [ProviderToolDefinition("inspect", "Read versions", {"type": "object"})]
    schema = text_tool_schema(tools)
    validator = Draft202012Validator(schema)
    assert validator.is_valid({"tool": "inspect", "args": {"rows": [{"revision": 1}]}})
    assert validator.is_valid({"tool": "inspect", "args": {"sources": ["url"], "evidence": [{"excerpt": "quote"}]}})
    assert not validator.is_valid({"tool": "inspect", "args": {}, "extra": True})
    tools.append(ProviderToolDefinition("later", "Later", {}))
    assert not validator.is_valid({"tool": "later", "args": {}})
    with pytest.raises(ValueError, match="declared tools"):
        text_tool_schema([])


def test_multiple_tools_bind_their_own_required_arguments_and_bounds():
    from codey.providers.base import tools_from_specs
    from codey.toolchain.tool_spec import spec_for_tool

    tools = tools_from_specs(tuple(spec_for_tool(name) for name in ("source_search", "open_hit", "research_plan")))
    validator = Draft202012Validator(text_tool_schema(tools))
    assert validator.is_valid({"tool": "source_search", "args": {"url": "https://sqlite.org/wal.html", "query": "writer"}})
    assert validator.is_valid({"tool": "open_hit", "args": {"hit_id": "h1"}})
    assert validator.is_valid({"tool": "research_plan", "args": {"questions": ["Writer limit?"]}})
    assert not validator.is_valid({"tool": "source_search", "args": {"query": "writer"}})
    assert not validator.is_valid({"tool": "open_hit", "args": {"url": "https://sqlite.org/wal.html", "query": "writer"}})
    assert not validator.is_valid({"tool": "research_plan", "args": {"questions": [str(n) for n in range(9)]}})


def test_multiple_strict_research_tools_require_done_coverage_state():
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.providers.base import tools_from_specs

    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "knowledge.write"}), strict_research=True),
                          task_kind="research")
    # Explicit legacy report contract, after questions have been registered.
    session.research_questions = ("What does the source establish?",)
    schema = text_tool_schema(tools_from_specs(build_turn_snapshot(session, native=False).model_specs))
    validator = Draft202012Validator(schema)
    coverage = {"question_ref": "q1", "state": "not_investigated",
                "findings": [], "investigations": []}
    raw = {"tool": "done", "args": {"report_sections": {"coverage": [coverage]}}}
    assert validator.is_valid(raw)
    del coverage["state"]
    assert not validator.is_valid(raw)


def test_strict_report_task_refs_bind_the_original_task_in_both_contracts():
    from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.providers.base import AssistantTurn, ProviderToolCall, tools_from_specs

    session = TaskSession(TaskPolicy(frozenset({"control"}), strict_research=True), task_kind="research",
                          task_text="Explain writers. Identify version gaps.")
    session.research_questions = ("Writer limits and version gaps?",)
    snapshot = build_turn_snapshot(session)
    validator = Draft202012Validator(text_tool_schema(tools_from_specs(snapshot.model_specs)))
    row = {"question_ref": "q1", "state": "not_investigated",
           "findings": [], "investigations": [], "task_refs": ["r1", "r2"]}
    raw = {"tool": "done", "args": {"report_sections": {"coverage": [row]}}}
    assert validator.is_valid(raw)
    for invalid in (["r999"], None):
        if invalid is None:
            row.pop("task_refs")
        else:
            row["task_refs"] = invalid
        assert not validator.is_valid(raw)
        plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("done", "done", raw["args"]),)), snapshot=snapshot)
        assert plan.control is None and "task_refs" in plan.protocol_error


@pytest.mark.parametrize("native", [False, True])
def test_report_question_choices_follow_registered_receipts_and_freeze_per_turn(native):
    from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.providers.base import AssistantTurn, ProviderToolCall, tools_from_specs

    session = TaskSession(TaskPolicy(frozenset({"control"}), strict_research=True), task_kind="research",
                          task_text="Investigate the substantive questions. Write a short report.")
    session.research_questions = tuple(f"Actual question {n}?" for n in range(1, 6))
    snapshot = build_turn_snapshot(session, native=native)
    validator = Draft202012Validator(text_tool_schema(tools_from_specs(snapshot.model_specs)))
    rows = [{"question_ref": f"q{n}", "state": "not_investigated",
             "findings": [], "investigations": [], "task_refs": ["r1", "r2"] if n == 1 else []}
            for n in range(1, 6)]
    raw = {"tool": "done", "args": {"report_sections": {"coverage": rows}}}
    assert validator.is_valid(raw)
    rows.append({**rows[-1], "question_ref": "q6"})
    assert not validator.is_valid(raw)
    turn = AssistantTurn(tool_calls=(ProviderToolCall("done", "done", raw["args"]),))
    rejected = normalize_turn(turn, snapshot=snapshot)
    assert rejected.control is None and "coverage" in rejected.protocol_error

    # Only a newly registered question may change the next frozen contract.
    session.research_questions += ("A newly registered substantive question?",)
    assert not validator.is_valid(raw)
    current = build_turn_snapshot(session, native=native)
    assert Draft202012Validator(text_tool_schema(tools_from_specs(current.model_specs))).is_valid(raw)
    assert normalize_turn(turn, snapshot=current).control is not None


@pytest.mark.parametrize("native", [False, True])
def test_format_requirement_needs_no_qn_or_task_ref_and_duplicate_repairs_are_rejected(native):
    from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.providers.base import AssistantTurn, ProviderToolCall, tools_from_specs

    session = TaskSession(TaskPolicy(frozenset({"control"}), strict_research=True), task_kind="research",
                          task_text="Explain concurrency. 给出约800字的中文研究结论。")
    session.research_questions = ("What is the limit?", "What is still unknown?")
    snapshot = build_turn_snapshot(session, native=native)
    validator = Draft202012Validator(text_tool_schema(tools_from_specs(snapshot.model_specs)))
    rows = [{"question_ref": f"q{n}", "state": "not_investigated",
             "findings": [], "investigations": [], "task_refs": ["r1"] if n == 1 else []} for n in (1, 2)]
    raw = {"tool": "done", "args": {"report_sections": {"coverage": rows}}}
    assert validator.is_valid(raw)
    rows[0]["task_refs"] = ["r2"]
    assert not validator.is_valid(raw)
    rows[0]["task_refs"] = ["r1"]
    rows.append({**rows[0], "task_refs": ["r1"]})
    assert not validator.is_valid(raw)
    result = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("done", "done", raw["args"]),)), snapshot=snapshot)
    assert result.control is None


def test_format_only_task_has_valid_empty_task_refs_schema():
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.providers.base import tools_from_specs

    session = TaskSession(TaskPolicy(frozenset({"control"}), strict_research=True), task_kind="research",
                          task_text="请用中文回答。")
    session.research_questions = ("Research context?",)
    schema = text_tool_schema(tools_from_specs(build_turn_snapshot(session).model_specs))
    Draft202012Validator.check_schema(schema)
    assert Draft202012Validator(schema).is_valid({"tool": "done", "args": {"report_sections": {"coverage": [{
        "question_ref": "q1", "state": "not_investigated", "findings": [],
        "investigations": [], "task_refs": []}]}}})


def test_explicit_schema_reaches_payload_but_does_not_leak_to_next_send(monkeypatch):
    payloads = []

    def generate(_url, payload, _headers, **_kwargs):
        payloads.append(payload)
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(api_generation, "generate", generate)
    provider = ApiProvider("http://fixture/v1", "fixture", native_tools=False, thinking_enabled=False,
                           text_tool_format="json_schema")
    try:
        schema = text_tool_schema([ProviderToolDefinition("inspect", "Read", {"type": "object"})])
        assert provider.send("task", tool_call_json=True, tool_call_schema=schema) == "ok"
        assert payloads[0]["response_format"]["json_schema"]["schema"] == schema
        assert provider.send("ordinary") == "ok"
        assert "response_format" not in payloads[1]
        with pytest.raises(ValueError, match="requires JSON"):
            provider.send("bad", tool_call_schema=schema)
    finally:
        provider.close()


@pytest.mark.parametrize("text_format", ["plain", "json_schema"])
def test_request_scoped_contract_is_counted_but_never_enters_history_or_auxiliary(monkeypatch, text_format):
    payloads = []
    counted = []

    def generate(_url, payload, _headers, **_kwargs):
        payloads.append(payload)
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(api_generation, "generate", generate)
    provider = ApiProvider("http://fixture/v1", "fixture", native_tools=False, thinking_enabled=False,
                           text_tool_format=text_format, system_prompt="BASE SYSTEM")
    counter = provider.request_counter

    def count(payload, **kwargs):
        counted.append(payload)
        return counter(payload, **kwargs)

    provider.request_counter = count
    auxiliary = api_context.fork_auxiliary(provider)
    try:
        provider.send("first result", tool_call_json=text_format == "json_schema", tool_call_contract="FROZEN FIRST CONTRACT",
                      tool_call_observations="RECEIPT SNAPSHOT ONE")
        provider.send("second result", tool_call_json=text_format == "json_schema", tool_call_contract="FROZEN SECOND CONTRACT",
                      tool_call_observations="RECEIPT SNAPSHOT TWO")
        provider.send("ordinary prose")
        auxiliary.send("summarize")
        assert "FROZEN FIRST CONTRACT" in str(payloads[0])
        assert "FROZEN FIRST CONTRACT" not in str(payloads[1:])
        assert "FROZEN SECOND CONTRACT" in str(payloads[1])
        assert "FROZEN SECOND CONTRACT" not in str(payloads[2:])
        assert payloads[1]["messages"][0]["content"] == "BASE SYSTEM\n\nFROZEN SECOND CONTRACT"
        assert "FROZEN" not in str(provider._messages)
        assert "RECEIPT SNAPSHOT" not in str(provider._messages)
        assert "RECEIPT SNAPSHOT ONE" not in str(payloads[1:])
        assert "RECEIPT SNAPSHOT TWO" not in str(payloads[2:])
        assert payloads[1]["messages"][-3:] == [
            {"role": "user", "content": "RECEIPT SNAPSHOT TWO"},
            {"role": "user", "content": "second result"}, payloads[1]["messages"][-1]]
        assert any("RECEIPT SNAPSHOT TWO" in str(payload) for payload in counted)
        assert any("FROZEN SECOND CONTRACT" in str(payload) for payload in counted)
        assert ("response_format" in payloads[0]) is (text_format == "json_schema")
    finally:
        auxiliary.close()
        provider.close()
