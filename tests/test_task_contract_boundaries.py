"""Published inventory and task contracts agree before any browser action executes."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from codey.knowledge.model import KnowledgeMutation, KnowledgeObject
from codey.knowledge.store import KnowledgeStore
from codey.launcher.runtime import read_runtime
from codey.operations.browser_upload import lower_upload_action, structured_upload_spec, task_upload_scope
from codey.operations.knowledge_context import knowledge_background
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.research.tools import ResearchTools
from codey.toolchain.tool_spec import spec_for_tool, validate_args_with_spec
from codey.types.json_value import JsonObject, decode_json_object


def test_checkout_inventory_includes_chat_contract_module() -> None:
    raw: object = read_runtime(Path(__file__).resolve().parents[1])
    descriptor = decode_json_object(raw)
    files = descriptor["files"]
    assert isinstance(files, list) and "codey/operations/chat_knowledge.py" in files


def checks(*requirements: JsonObject) -> tuple[str, ...]:
    return tuple(require_tool_success("browser_wait", via="codemode", arguments=args) for args in requirements)


@pytest.mark.parametrize("extra,missing", [({}, False), ({"value": "received"}, False),
    ({"value": "processed"}, True), ({"condition": "selector", "value": "#processed"}, False)])
def test_wait_contract_covers_each_declared_predicate(extra: JsonObject, missing: bool) -> None:
    required = checks({"condition": "text", "value": "received"}, extra)
    scope = task_upload_scope("chat", "Upload the attachment", frozenset({"https://example.com/"}),
                              TaskPolicy(frozenset({"browser.effect"}), required_checks=required))
    assert scope is not None
    spec = spec_for_tool("codemode")
    assert spec is not None
    model = structured_upload_spec(spec, scope, "read")
    assert bool(validate_args_with_spec(model, {"upload": {"action": "read"}})) is missing
    if missing:
        with pytest.raises(ValueError, match="wait"):
            lower_upload_action({"upload": {"action": "read"}}, required)
        with pytest.raises(ValueError, match="wait"):
            lower_upload_action({"upload": {"action": "read", "wait_for_text": "wrong"}}, required)
        converted = lower_upload_action({"upload": {"action": "read", "wait_for_text": "processed"}}, required)
        assert decode_json_object(converted["inputs"])["waits"] == [
            {"condition": "text", "value": "received", "timeout_ms": 5000},
            {"condition": "text", "value": "processed", "timeout_ms": 5000}]


def test_multiple_partial_waits_can_be_completed_with_canonical_wait_arguments() -> None:
    required = checks({"condition": "text", "value": "received"}, {"value": "processed"}, {"condition": "selector"})
    action: JsonObject = {"upload": {"action": "read", "waits": [
        {"condition": "text", "value": "processed", "timeout_ms": 2000},
        {"condition": "selector", "value": "#ready", "timeout_ms": 3000}]}}
    scope = task_upload_scope("chat", "Upload the attachment", frozenset({"https://example.com/"}),
                              TaskPolicy(frozenset({"browser.effect"}), required_checks=required))
    assert scope is not None
    spec = spec_for_tool("codemode")
    assert spec is not None
    assert not validate_args_with_spec(structured_upload_spec(spec, scope, "read"), action)
    converted = lower_upload_action(action, required)
    waits = decode_json_object(converted["inputs"])["waits"]
    assert isinstance(waits, list) and len(waits) == 3


@pytest.mark.parametrize("search", [False, True])
def test_chat_background_and_search_require_reading_before_citing(tmp_path: Path, search: bool) -> None:
    with closing(KnowledgeStore(tmp_path / "knowledge")) as store:
        store.repository.commit("seed", [KnowledgeMutation(KnowledgeObject(
            "saved-observation", "observation", "Pricing observation marker.", "observed_result"))])
        tools = ResearchTools(None, store, None)
        session = TaskSession(TaskPolicy(frozenset({"control", "knowledge.read"})), task_kind="chat")
        text = tools.knowledge_search("Pricing").model_text if search else knowledge_background(tools, session, "Pricing")
        assert "saved-observation" in text and "knowledge_read" in text
        assert "When using saved versions, include knowledge_refs" not in text
