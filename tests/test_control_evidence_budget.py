"""Nested evidence must obey the byte budget, not only top-level strings."""

import json
from types import SimpleNamespace

from codey.app.control_queries import evidence_page


def test_nested_tool_evidence_is_bounded():
    entry = SimpleNamespace(entry_id="e", kind="tool_result", by_task_id="r", payload={"data": {"content": "证据" * 300000}})
    durable = SimpleNamespace(task=lambda _: SimpleNamespace(conversation_id="c"), entries=lambda _: [entry])
    result = evidence_page(SimpleNamespace(durable=durable), {"conversation_id": "c", "run_id": "r"})
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 128 * 1024
    assert result["items"][0]["truncated"]
