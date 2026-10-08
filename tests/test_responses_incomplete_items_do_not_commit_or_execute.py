"""A terminal response cannot launder an explicitly unfinished output item."""
from __future__ import annotations

import pytest

from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolDefinition


@pytest.mark.parametrize("kind", ["function_call", "message"])
@pytest.mark.parametrize("status", ["in_progress", "incomplete", "failed", "unknown"])
def test_incomplete_output_item_never_reaches_kernel_or_commits_history(monkeypatch, kind, status):
    provider = ApiProvider("http://fixture.test/v1", "fixture", api_protocol="openai-responses")
    item = {"type": kind, "status": status}
    if kind == "function_call":
        item.update(call_id="write1", name="edit", arguments='{"path":"a.py","content":"changed"}')
    else:
        item.update(role="assistant", content=[{"type": "output_text", "text": "complete"}])
    monkeypatch.setattr(api_transport, "generate", lambda *a, **kw: {"status": "completed", "output": [item]})
    with pytest.raises(RuntimeError, match="output item.*not complete"):
        provider.send_turn("Edit a.py", [ProviderToolDefinition("edit", "edit", {})])
    assert not provider.has_transport_history
    assert not provider._declared_tools
