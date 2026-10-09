"""Portable work state crosses model/protocol identities without opaque wire items."""
from unittest.mock import patch

from codey.providers.api_provider import ApiProvider


def test_new_model_reads_saved_work_state_without_importing_old_protocol_history(tmp_path):
    original = ApiProvider("http://localhost:9/v1", "model-a", context_keep_recent_tokens=1000)
    original.bind_context("session-a", tmp_path)
    original._messages = [{"role": "user", "content": "Keep API"},
                          {"role": "assistant", "content": "details " * 4000},
                          {"role": "user", "content": "Latest correction: edit corrected.py only"}]
    original.summarize_context = lambda *_: "Goal: Keep API. Next: read exec-17."
    original.maintain_context()
    original.wait_for_maintenance(3)
    original.close()
    switched = ApiProvider("http://localhost:9/v1", "model-b", api_protocol="openai-responses")
    switched.bind_context("session-a", tmp_path)
    assert switched._messages == []
    assert "Keep API" in switched.export_work_state()
    assert "exec-17" in switched.export_work_state()
    assert "corrected.py" in switched.export_work_state()
    assert "encrypted_content" not in switched.export_work_state()
    with patch("codey.providers.api_transport.generate", return_value={"status": "completed", "output": []}):
        switched.send("continue")


def test_portable_state_keeps_all_segment_checkpoints_before_the_recent_tail():
    from codey.providers.context_ledger import digest
    provider = ApiProvider("http://localhost:9/v1", "model-a", context_keep_recent_tokens=1000)
    checkpoint = provider._codec.checkpoint_item("Binding constraint: no-db")
    view = [checkpoint, *[{"role": "assistant", "content": f"recent step {i}"} for i in range(20)]]
    provider.context_ledger.commit(view, checkpoint={"item_digest": digest(checkpoint), "source": [], "text": "Binding constraint: no-db"})
    assert "no-db" in provider.export_work_state()
