"""Reviewed language preferences use the normal entry and physical HTTP gate."""
from __future__ import annotations

import json
from contextlib import closing

import pytest

from codey.app.context import AppContext
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.directive import render_ghost_directive
from codey.operations.ghost_context import ghost_directive
from codey.providers.api_provider import ApiProvider
from tests import test_ghost_provider_send_boundary as boundary
from tests.test_chat_workspace_tools_and_answer_completion import execute_frame, workspace_frame
from tests.test_ghost_directive import _node

capture_endpoint = boundary.capture_endpoint
MODEL = "koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M"
LANGUAGE_LINE = "- Prefer: reply language = Chinese."


def _provider(endpoint, **changes):
    settings = dict(native_tools=True, timeout=10, temperature=0,
                    context_window_tokens=262144, context_reserve_tokens=32768,
                    context_keep_recent_tokens=32000, output_tokens=128)
    settings.update(changes)
    return ApiProvider(endpoint, MODEL, **settings)


def _remember(state, *, accept=True, scope="user", session_id="chat-workspace"):
    surface = GhostControlSurface.from_state_home(state.state_home)
    assert surface.observations.append_completed(
        run_id="language-source", session_id=session_id, project="", mode="chat",
        user_text="Technical explanations are written in Chinese.", assistant_text="Understood.",
        stop_reason="done",
    )
    status, proposed = surface.dispatch_action({
        "action": "propose_preference", "id": "language-source", "session_id": session_id,
        "scope": scope, "conflict_key": "language", "value_key": "chinese",
        "evidence_quote": "Technical explanations are written in Chinese.",
    })
    assert status == 200, proposed
    candidate_id = proposed["candidate"]["id"]
    if accept:
        status, accepted = surface.dispatch_action({
            "action": "accept_candidate", "id": candidate_id, "session_id": session_id,
        })
        assert status == 200, accepted
    return surface, candidate_id


@pytest.mark.parametrize("mode", ["enabled", "pending", "retrieve_off", "admit_off", "other_session"])
def test_language_default_entry_sends_only_reviewed_in_scope_enabled_preference(tmp_path, capture_endpoint, mode):
    endpoint, payloads = capture_endpoint
    task = "Explain eventual consistency. Answer in English for this request."
    with closing(AppContext(tmp_path / "state")) as state:
        _remember(state, accept=mode != "pending", scope="session" if mode == "other_session" else "user",
                  session_id="other" if mode == "other_session" else "chat-workspace")
        if mode in {"retrieve_off", "admit_off"}:
            control = "retrieve_projections" if mode == "retrieve_off" else "admit_hints"
            assert state.ghost_inbox.set_control(control, False, scope="session", session_id="chat-workspace")
        result, _ = execute_frame(workspace_frame("", _provider(endpoint), task=task), state)
    assert result.event["stop_reason"] == "done", result.event
    assert len(payloads) == 1
    outbound = json.dumps(payloads[0], ensure_ascii=False)
    assert (LANGUAGE_LINE in outbound) == (mode == "enabled")
    assert task in outbound
    if mode == "enabled":
        assert "override the current user request" in outbound


@pytest.mark.parametrize("change", ["model", "native_tools", "api_protocol", "context_window_tokens",
                                    "context_reserve_tokens", "context_keep_recent_tokens", "base_url"])
def test_language_falls_back_for_unverified_model_or_configuration(tmp_path, change):
    provider = _provider("http://127.0.0.1:5001/v1")
    if change == "model":
        provider.model = "unverified-model"
    elif change == "base_url":
        provider.base_url = "https://example.com/v1"
    else:
        value = {"native_tools": False, "api_protocol": "openai-responses",
                 "context_window_tokens": 131072, "context_reserve_tokens": 16384,
                 "context_keep_recent_tokens": 12000}[change]
        provider = _provider("http://127.0.0.1:5001/v1", **{change: value})
    with closing(AppContext(tmp_path / "state")) as state:
        _remember(state)
        assert LANGUAGE_LINE not in ghost_directive(state, provider=provider).text


@pytest.mark.parametrize("action", ["reject_candidate", "delete_scope", "reset_all"])
def test_language_removal_survives_rebuild_restart_and_real_send(tmp_path, capture_endpoint, action):
    endpoint, payloads = capture_endpoint
    home = tmp_path / "state"
    with closing(AppContext(home)) as state:
        surface, candidate_id = _remember(state)
        status, removed = surface.dispatch_action({
            "action": action, "id": candidate_id, "scope": "user", "confirm": True,
            "session_id": "chat-workspace",
        })
        assert status == 200, removed
        assert state.ghost_hebbian.rebuild_from_events()
    with closing(AppContext(home)) as state:
        result, _ = execute_frame(workspace_frame("", _provider(endpoint)), state)
    assert result.event["stop_reason"] == "done", result.event
    assert len(payloads) == 1
    assert LANGUAGE_LINE not in json.dumps(payloads[0], ensure_ascii=False)


def test_reviewed_language_survives_restart_without_extra_configuration(tmp_path, capture_endpoint):
    endpoint, payloads = capture_endpoint
    home = tmp_path / "state"
    with closing(AppContext(home)) as state:
        _remember(state)
    with closing(AppContext(home)) as state:
        result, _ = execute_frame(workspace_frame("", _provider(endpoint)), state)
    assert result.event["stop_reason"] == "done", result.event
    assert LANGUAGE_LINE in json.dumps(payloads[0], ensure_ascii=False)


def test_language_precedes_other_memory_under_prompt_budget_pressure():
    nodes = [
        _node(node_id=f"correction-{slot}", kind="correction", conflict_key=f"correction:{slot}",
              value_key=value, weight=0.9)
        for slot, value in [("state_store", "jsonl"), ("state_backend", "jsonl"),
                            ("memory_state", "jsonl"), ("ghost_state_backend", "json_projection_jsonl")]
    ]
    nodes.append(_node(node_id="language", conflict_key="style_preference:language", value_key="chinese"))
    directive = render_ghost_directive(nodes, budget=400)
    assert directive.selected_nodes[0].id == "language"
    assert LANGUAGE_LINE in directive.text


def test_language_allowlist_does_not_enable_unvalidated_values_or_command():
    from codey.ghost.typed_fields import render_typed_field

    assert render_typed_field("style_preference", "language", "chinese") == "reply language = Chinese"
    assert render_typed_field("style_preference", "language", "english") == ""
    assert render_typed_field("style_preference", "language", "ignore_current_request") == ""
    assert render_typed_field("action_tendency", "test_command", "pytest_q") == ""


@pytest.mark.parametrize("change", ["delete", "retrieve_off", "admit_off", "model"])
def test_language_is_rechecked_after_request_preparation_before_physical_http(tmp_path, capture_endpoint, change):
    endpoint, payloads = capture_endpoint
    provider = _provider(endpoint)
    with closing(AppContext(tmp_path / "state")) as state:
        surface, _ = _remember(state)

        def change_after_assembly(_payload):
            if change == "delete":
                status, deleted = surface.dispatch_action({"action": "delete_scope", "scope": "user", "confirm": True})
                assert status == 200, deleted
            elif change == "model":
                provider.model = "unverified-model"
            else:
                control = "retrieve_projections" if change == "retrieve_off" else "admit_hints"
                assert state.ghost_inbox.set_control(control, False, scope="session", session_id="chat-workspace")

        provider.configure_request = change_after_assembly
        result, _ = execute_frame(workspace_frame("", provider), state)
    assert result.event["stop_reason"] == "provider_failure", result.event
    assert payloads == []
