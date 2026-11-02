"""Exact-version traversal and lossless historical kernel record decoding."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codey.knowledge.model import KnowledgeMutation as M
from codey.knowledge.recall import KnowledgeAccess, KnowledgeRecall
from codey.knowledge.store import KnowledgeStore
from codey.operations.kernel_contracts import KernelResult, result_from_payload
from codey.operations.kernel_receipts import restore_result_receipt, result_receipt_fields, text_digest
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.effects.effect_records import compute_args_digest
from codey.storage.managed_outputs import ManagedOutputStore
from tests.test_knowledge_repository import example


def test_reverse_neighbors_match_the_requested_revision_after_restart(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        store.repository.commit("initial", example())
        source = store.repository.read("source")
        store.repository.commit("new-source", [M(replace(source.value, content="New text"), 1)])
    finally:
        store.close()
    store = KnowledgeStore(tmp_path)
    try:
        recall = KnowledgeRecall(store.repository)
        assert ("excerpt", 1, "from_source") in recall.neighbors(store.repository.read("source", 1), KnowledgeAccess())
        assert recall.neighbors(store.repository.read("source", 2), KnowledgeAccess()) == []
    finally:
        store.close()


@pytest.mark.parametrize("shape", ["six", "seven", "versioned"])
def test_known_kernel_result_formats(shape):
    original = KernelResult(False, "Preserved failure", 3, "protocol", delivery="unknown")
    payload = original.to_payload()
    if shape != "versioned":
        del payload["schema_version"]
    if shape == "six":
        del payload["research_report"]
    assert result_from_payload(payload) == original
    assert result_from_payload(result_from_payload(payload).to_payload()) == original


@pytest.mark.parametrize("extra", [{"schema_version": True}, {"schema_version": 99}, {"unknown": None}])
def test_kernel_result_rejects_unknown_versions_and_fields(extra):
    with pytest.raises(ValueError):
        result_from_payload({**KernelResult(False, "failure", 0, "protocol").to_payload(), **extra})


@pytest.mark.parametrize("fault", ["", "effect", "digest", "truncated", "session", "format", "payload"])
def test_legacy_receipt_checks_raw_bytes_scope_effect_and_digest(tmp_path, fault, monkeypatch):
    store = ManagedOutputStore(tmp_path)
    result = ToolResult(ToolCall("open_url", {"url": "https://example.org"}, call_id="c"), "中文\r\n" * 2500, ok=True)
    fields = result_receipt_fields(result, store=store, session_id="s", run_id="r", effect_id="effect")
    data = store.read_json_artifact({"session_id": "s", "run_id": "r", "handle": fields["result_ref"],
                                    "schema_version": 2, "sha256": fields["result_payload_sha256"]})
    # Reorder deliberately: the old writer did not sort keys.
    raw = json.dumps(dict(reversed(list(data.items()))), ensure_ascii=False, separators=(",", ":"))
    receipt = store.write_tool_output(session_id="s", run_id="r", tool_id="wrong" if fault == "effect" else "effect",
                                     permission_profile="coding_writer", tool_name="tool_result", display_ref="effect", text=raw)
    fields.update(result_payload="", result_ref=receipt.handle, result_payload_sha256=text_digest(raw))
    if fault == "digest":
        fields["result_payload_sha256"] = "a" * 64
    if fault == "format":
        fields["result_ref"] = "unknown_0001"
    if fault == "truncated":
        metadata = store.metadata_path_for("s", "r", receipt.handle)
        value = json.loads(metadata.read_text(encoding="utf-8"))
        metadata.write_text(json.dumps({**value, "stored_truncated": True}), encoding="utf-8")
    projection = SimpleNamespace(intent=SimpleNamespace(tool_name="open_url", effect_id="effect", call_id="c",
        args_digest=compute_args_digest(result.call.args)), settlement=SimpleNamespace(**fields, status="ok"))
    if fault == "payload":
        _, metadata = store.read_tool_output("s", "r", receipt.handle)
        monkeypatch.setattr(store, "read_tool_output", lambda *args: ({"not": "text"}, metadata))
        with pytest.raises(ValueError, match="legacy result receipt payload must be text"):
            restore_result_receipt(projection, store=store, session_id="s", run_id="r")
        return
    if fault:
        with pytest.raises((ValueError, OSError)):
            restore_result_receipt(projection, store=store, session_id="other" if fault == "session" else "s", run_id="r")
    else:
        assert restore_result_receipt(projection, store=store, session_id="s", run_id="r") == result
