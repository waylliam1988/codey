from __future__ import annotations

import json

from codey.providers.api_codec import GenerationSettings
from codey.providers.api_provider import ApiProvider
from codey.providers.request_metrics import first_changed_section, request_shape
from codey.providers.task_prompt import TASK_HEADING, retry_task_prompt
from codey.types.json_value import decode_json_object


def test_final_api_window_keeps_one_task_and_all_repair_instructions() -> None:
    provider = ApiProvider("http://localhost:5001/v1", "test")
    settings = GenerationSettings("test", False, 0, None, None, "auto", None)
    task = "Configure the parser.\n\nKeep its default encoding."
    original = {"role": "user", "content": TASK_HEADING + task + "\n\nWorkspace: ."}
    retries = [
        {"role": "user", "content": retry_task_prompt(task, repair, scoped=True)}
        for repair in ("First repair.", "Second repair.")
    ]
    try:
        for window in ([original, *retries], retries, retries[1:]):
            messages: list[dict[str, object]] = [dict(item) for item in window]
            payload = provider._payload(messages, None, settings, clock="fixed clock")
            projected = json.dumps(payload)
            assert projected.count(json.dumps(task)[1:-1]) == 1
            assert all(repair in projected for repair in ("First repair.", "Second repair.")
                       if any(repair in item["content"] for item in window))
            assert "Codey retry task" not in projected
            assert messages == window
    finally:
        provider.close()


def test_request_metrics_separate_system_schema_and_effective_fields() -> None:
    first = b'{"messages":[{"role":"system","content":"rules"}],"response_format":{},"max_tokens":5}'
    second = first.replace(b"rules", b"updated")
    sections, fields = request_shape(first)
    updated, _ = request_shape(second)
    assert [section.name for section in sections] == ["system", "response_format"]
    assert sections[0].characters == 5 and len(sections[0].digest) == 64
    assert fields == decode_json_object({"max_tokens": 5})
    assert first_changed_section(sections, updated) == "system"
