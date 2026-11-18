"""Strict native labels, source admission, and one-request HTTP/SSE behavior."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.agents.request import AgentRequest
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.payload_types import decoded_object, mapping_fields
from codey.providers.base import AssistantTurn, ProviderToolCall, TurnFinish
from codey.types.json_value import decode_json_object
from codey.workspace.context_admission import request_scope, sources_from_values
from codey.workspace.revision import WorkspaceRevisionStore
from tests.manual.ghost_memory_routing import portable_context_native_trial as trial
from tests.manual.ghost_memory_routing import portable_context_trial as legacy
from tests.test_ghost_portable_context_trial import ANSWER, WireEndpoint
from tests.test_ghost_portable_context_trial import wire_endpoint as wire_endpoint


def turn(arguments: object, *, name: str = trial.TOOL_NAME) -> AssistantTurn:
    return AssistantTurn(tool_calls=(ProviderToolCall("call-1", name, decode_json_object(arguments)),))


def test_direct_native_arguments_do_not_need_a_summary_string() -> None:
    assert trial.decode_probe(turn(ANSWER)) == ANSWER
    schema = trial.probe_tool().parameters
    assert schema["required"] == list(ANSWER) and schema["additionalProperties"] is False
    properties = schema["properties"]
    assert mapping_fields(properties) and "summary" not in properties


@pytest.mark.parametrize("arguments", (
    {**ANSWER, "tone": "warm and considerate"}, {**ANSWER, "reason": "offline_cursor_sync preserves the cursor"},
    {**ANSWER, "choice": 1}, {**ANSWER, "extra": "warm"}, {"choice": "opalpanel"},
    {"summary": "{'choice': 'opalpanel', 'tone': 'warm'}"},
))
def test_native_fields_reject_nonlabels_extra_fields_and_nested_dictionary_text(arguments: object) -> None:
    with pytest.raises(ValueError):
        trial.decode_probe(turn(arguments))


@pytest.mark.parametrize("failure", ("wrong_tool", "multiple_calls", "no_call", "truncated", "prose", "no_id"))
def test_native_protocol_failures_are_not_repaired(failure: str) -> None:
    good = turn(ANSWER)
    variants = {
        "wrong_tool": turn(ANSWER, name="done"),
        "multiple_calls": AssistantTurn(tool_calls=good.tool_calls * 2),
        "no_call": AssistantTurn(),
        "truncated": AssistantTurn(tool_calls=good.tool_calls, finish=TurnFinish.OUTPUT_LIMIT),
        "prose": AssistantTurn(text="Here is the answer", tool_calls=good.tool_calls),
        "no_id": AssistantTurn(tool_calls=(ProviderToolCall("", trial.TOOL_NAME, decode_json_object(ANSWER)),)),
    }
    with pytest.raises(ValueError):
        trial.decode_probe(variants[failure])


@pytest.mark.parametrize("stream", (False, True))
@pytest.mark.parametrize("arm", legacy.ARMS)
def test_native_cell_over_real_http_uses_one_request_and_preserves_unknown_usage(
    tmp_path: Path, wire_endpoint: WireEndpoint, stream: bool, arm: str,
) -> None:
    wire_endpoint.tool_name = trial.TOOL_NAME
    wire_endpoint.omit_usage = True
    wire_endpoint.arguments = ({"choice": "unknown", "reason": "unknown", "priority": "unknown", "tone": "neutral"}
                               if arm == "off" else {**ANSWER, "priority": "animations", "tone": "technical"}
                               if arm == "override" else dict(ANSWER))
    home = tmp_path / "cell"
    with patch.object(legacy, "ENDPOINT", wire_endpoint.url):
        result = trial.cell(home, output=8192, stream=stream, arm=arm)
    assert result["format_valid"] is True and result["requests"] == 1
    assert result["baseline_unknown"] is (arm == "off")
    assert result["recall_correct"] is (arm != "off")
    assert result["sources_present"] is (arm != "off")
    assert result["usage_status"] == "missing"
    assert result["input_tokens"] is None and result["generated_tokens"] is None
    payload = wire_endpoint.requests[0]
    assert len(wire_endpoint.requests) == 1
    tools = payload["tools"]
    assert isinstance(tools, list) and len(tools) == 1
    entries: list[object] = tools
    declared = decode_json_object(entries[0])
    function = decode_json_object(declared["function"])
    assert function["name"] == trial.TOOL_NAME
    params = decode_json_object(function["parameters"])
    assert set(decode_json_object(params["properties"])) == set(ANSWER)


def test_invalid_native_labels_stop_after_one_request_and_retain_reply(
    tmp_path: Path, wire_endpoint: WireEndpoint,
) -> None:
    wire_endpoint.tool_name = trial.TOOL_NAME
    wire_endpoint.arguments = {**ANSWER, "tone": "warm and considerate"}
    with patch.object(legacy, "ENDPOINT", wire_endpoint.url), pytest.raises(ValueError, match="invalid label"):
        trial.cell(tmp_path / "cell", output=8192, stream=True, arm="on")
    assert len(wire_endpoint.requests) == 1
    assert (tmp_path / "cell/native-reply.json").is_file()
    assert decoded_object((tmp_path / "cell/attempt.json").read_text())["physical_requests"] == 1


@pytest.mark.parametrize("boundary", ("disabled", "deleted", "revoked"))
def test_physical_admission_rereads_controls_and_source_authority(tmp_path: Path, boundary: str) -> None:
    home, project = tmp_path / "state", tmp_path / "project"
    project.mkdir()
    legacy.seed(home, project)
    surface = GhostControlSurface.from_state_home(home)
    assert surface.inbox is not None and surface.observations is not None
    with closing(legacy.CapturedProvider(tmp_path, output=8192, stream=True)) as provider:
        request = AgentRequest(provider, project, trial.TASK, session_id="next", run_id="next",
                               workspace_revision_store=WorkspaceRevisionStore(home))
        sources = trial.read_sources(request)
        prompt = "\n\n".join(source.text for source in sources)
        message: dict[str, object] = {"role": "user", "content": prompt}
        payload: dict[str, object] = {"messages": [message]}
        trial.admit_payload(request, sources, prompt, payload)
        if boundary == "disabled":
            assert surface.inbox.set_control("admit_hints", False)
        elif boundary == "deleted":
            status, result = surface.dispatch_action({"action": "reset_all", "confirm": True})
            assert status == 200 and result["ok"] is True
        else:
            candidate = next(row for row in surface.inbox.list_candidates() if row.signal_kind == "style_preference")
            status, result = surface.dispatch_action({"action": "reject_candidate", "id": candidate.id})
            assert status == 200 and result["ok"] is True
        with pytest.raises(ValueError, match="sources changed"):
            trial.admit_payload(request, sources, prompt, payload)
        assert provider.requests == 0


def test_admission_rejects_wrong_physical_prompt_and_scope(tmp_path: Path) -> None:
    with closing(legacy.CapturedProvider(tmp_path, output=8192, stream=True)) as provider:
        request = AgentRequest(provider, tmp_path, trial.TASK, session_id="next", admit_hints=False)
        message: dict[str, object] = {"role": "user", "content": "another prompt"}
        payload: dict[str, object] = {"messages": [message]}
        with pytest.raises(ValueError, match="prompt differs"):
            trial.admit_payload(request, (), trial.TASK, payload)
        sources = sources_from_values(directive="preference", scope=request_scope(tmp_path / "other", "next"))
        with pytest.raises(ValueError, match="sources changed"):
            trial.admit_payload(request, sources, trial.TASK, payload)
        assert provider.requests == 0
