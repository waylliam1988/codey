from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from codey.providers.api_codec import GenerationSettings
from codey.providers.api_provider import ApiProvider
from codey.providers.request_codec import decode_provider_request, encode_provider_request
from codey.providers.request_metrics import first_changed_section, request_shape
from codey.providers.requests import ProviderRequestOptions, SendTextRequest
from codey.providers.task_prompt import TASK_HEADING, TASK_PROMPT_KEY, TaskPromptSpan
from codey.types.json_value import decode_json_object


def test_user_message_resembling_legacy_retry_header_is_sent_verbatim() -> None:
    provider = ApiProvider("http://localhost:5001/v1", "test")
    settings = GenerationSettings("test", False, 0, None, None, "auto", None)
    text = "Codey retry task (3 characters):\nabc\n\nCopied log"
    message: dict[str, object] = {"role": "user", "content": text}
    try:
        payload = provider._payload([message], None, settings, clock="fixed clock")
        assert payload["messages"] == [message, {"role": "user", "content": "fixed clock"}]
    finally:
        provider.close()


def _task_message(task: str, trailing: str, *, retry: bool, run_id: str = "run") -> dict[str, object]:
    span = TaskPromptSpan.for_task(run_id, task, retry=retry)
    assert span is not None
    return {"role": "user", "content": TASK_HEADING + task + "\n\n" + trailing, TASK_PROMPT_KEY: asdict(span)}


@pytest.mark.parametrize(("protocol", "wire_key"), [("openai-completions", "messages"), ("openai-responses", "input")])
def test_final_api_window_keeps_one_task_and_all_repair_instructions(protocol: str, wire_key: str) -> None:
    provider = ApiProvider("http://localhost:5001/v1", "test", api_protocol=protocol)
    settings = GenerationSettings("test", False, 0, None, None, "auto", None)
    task = "Configure the parser.\n\n保留默认编码。"
    original = _task_message(task, "Workspace: .", retry=False)
    retries = [
        _task_message(task, repair, retry=True)
        for repair in ("First repair.", "Second repair.")
    ]
    try:
        for window in ([original, *retries], retries, retries[1:]):
            messages: list[dict[str, object]] = [dict(item) for item in window]
            payload = provider._payload(messages, None, settings, clock="fixed clock")
            projected = json.dumps(payload[wire_key])
            assert projected.count(json.dumps(task)[1:-1]) == 1
            for item in window:
                content = item["content"]
                assert isinstance(content, str)
                assert content.rsplit("\n\n", 1)[-1] in projected
            assert TASK_PROMPT_KEY not in projected
            assert messages == window
    finally:
        provider.close()


def test_frozen_retry_survives_cold_send_without_using_unmarked_or_other_run_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.providers.compat_transport import execute_compat_request

    provider = ApiProvider("http://localhost:5001/v1", "test")
    settings = GenerationSettings("test", False, 0, None, None, "auto", None)
    task = "Fix this parser."
    span = TaskPromptSpan.for_task("run", task, retry=True)
    frozen = encode_provider_request(SendTextRequest(
        TASK_HEADING + task + "\n\nRepair instruction", ProviderRequestOptions(task_prompt_span=span),
    ))
    recovered = decode_provider_request(frozen)
    assert isinstance(recovered, SendTextRequest) and recovered.options.task_prompt_span == span
    original = _task_message(task, "Previous run", retry=False, run_id="previous")
    pasted = {"role": "user", "content": TASK_HEADING + task + "\n\nPasted transcript"}
    sent: list[object] = []

    def capture(pending: list[dict[str, object]], _tools: object, **_options: object) -> object:
        from codey.providers.base import AssistantTurn

        sent.append(provider._payload([original, pasted, *pending], None, settings, clock="fixed clock")["messages"])
        return AssistantTurn(text="done")

    try:
        monkeypatch.setattr(provider, "_exchange", capture)
        assert execute_compat_request(provider, recovered) == "done"
    finally:
        provider.close()
    assert sent == [[
        {"role": "user", "content": original["content"]},
        pasted,
        {"role": "user", "content": recovered.prompt},
        {"role": "user", "content": "fixed clock"},
    ]]


def test_retry_projection_preserves_attachment_parts() -> None:
    provider = ApiProvider("http://localhost:5001/v1", "test")
    settings = GenerationSettings("test", False, 0, None, None, "auto", None)
    task = "Fix this parser."
    original = _task_message(task, "Workspace: .", retry=False)
    retry = _task_message(task, "Repair instruction", retry=True)
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
    retry["content"] = [{"type": "text", "text": retry["content"]}, image]
    try:
        payload = provider._payload([original, retry], None, settings, clock="fixed clock")
    finally:
        provider.close()
    assert payload["messages"] == [
        {"role": "user", "content": original["content"]},
        {"role": "user", "content": [{"type": "text", "text": "Repair instruction"}, image]},
        {"role": "user", "content": "fixed clock"},
    ]


def test_request_metrics_separate_system_schema_and_effective_fields() -> None:
    first = b'{"messages":[{"role":"system","content":"rules"}],"response_format":{},"max_tokens":5}'
    second = first.replace(b"rules", b"updated")
    sections, fields = request_shape(first)
    updated, _ = request_shape(second)
    assert [section.name for section in sections] == ["system", "response_format"]
    assert sections[0].characters == 5 and len(sections[0].digest) == 64
    assert fields == decode_json_object({"max_tokens": 5})
    assert first_changed_section(sections, updated) == "system"
