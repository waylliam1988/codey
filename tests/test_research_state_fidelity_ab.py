"""The presentation experiments cannot invent reads, versions or edit effects."""

import json
from types import SimpleNamespace

from codey.knowledge.model import KnowledgeObject
from codey.runtime.core.models import ToolCall, ToolResult
from tests.manual.research_state_fidelity_ab import edit_context, navigation, pending_references


def read(identity, revision, *, kind="claim", references=(), ok=True):
    origin = "model_inference" if kind == "inference" else "model_extraction"
    value = KnowledgeObject(identity, kind, "Original condition", origin).to_dict()
    return ToolResult(ToolCall("knowledge_read", {"id": identity, "revision": revision}),
                      json.dumps({**value, "revision": revision, "references": references, "content_complete": True}),
                      ok=ok)


def test_links_keep_pinned_versions_and_do_not_adopt_or_read_them():
    reference = {"target": "premise-with-hyphens", "revision": 2, "relation": "depends_on"}
    session = SimpleNamespace(_memory_results={"root": read("root", 1, kind="inference", references=[reference]),
                                               "newer": read("premise-with-hyphens", 3)})
    before = dict(session._memory_results)
    links = pending_references(session)
    assert links[0]["args"] == {"id": "premise-with-hyphens", "revision": 2}
    assert session._memory_results == before
    session._memory_results["failed"] = read("premise-with-hyphens", 2, ok=False)
    assert pending_references(session) == links
    session._memory_results["exact"] = read("premise-with-hyphens", 2)
    assert pending_references(session) == []


def test_edit_template_uses_original_protocol_and_success_survives_later_old_read():
    original = read("old-id", 4)
    session = SimpleNamespace(_memory_results={"read": original})
    args = edit_context(session)["edit_templates_not_executed"][0]["args"]
    assert args["changes"][0]["expected_revision"] == 4
    assert args["changes"][0]["value"]["id"] == "old-id"
    args["changes"][0]["value"]["title"] = "New title"
    session._memory_results["commit"] = ToolResult(ToolCall("knowledge_commit", args),
        json.dumps({"commit_id": "saved", "versions": {"old-id": 5}}), ok=True)
    session._memory_results["later_old_read"] = original
    state = edit_context(session)
    assert state["edit_templates_not_executed"] == []
    assert state["already_saved_changes"][0]["receipt"]["versions"] == {"old-id": 5}
    assert state["already_saved_changes"][0]["changes"][0]["value"]["title"] == "New title"
    assert json.loads(original.model_text)["revision"] == 4


def test_failed_commit_and_incomplete_read_do_not_claim_a_saved_edit():
    result = read("old-id", 1)
    value = json.loads(result.model_text)
    value["content_complete"] = False
    session = SimpleNamespace(_memory_results={
        "read": ToolResult(result.call, json.dumps(value), ok=True),
        "failed": ToolResult(ToolCall("knowledge_commit", {}), "ERROR: conflict", ok=False)})
    assert edit_context(session) == {"edit_templates_not_executed": [], "already_saved_changes": []}


def test_navigation_distinguishes_holes_partial_sections_and_complete_reads():
    locator = {"url": "https://example.org/article", "source_id": "s1", "available_length": 300,
               "read_intervals": [[0, 150], [200, 300]], "next_offset": 150,
               "sections": [{"label": "First", "offset": 0}, {"label": "Middle", "offset": 100},
                            {"label": "Last", "offset": 200}]}
    output = navigation(locator)
    assert [r["status"] for r in output["sections"]] == ["read", "partial", "read"]
    assert output["sections"][1]["read"]["args"] == {"url": locator["url"], "offset": 100}
    assert output["next_read"]["args"]["offset"] == 150
    assert "read" not in output["sections"][2]
    assert locator["source_id"] == "s1"
    stored = navigation({**locator, "knowledge_source": ["pinned-source", 2]})
    assert stored["next_read"] == {"tool": "knowledge_read", "args": {
        "id": "pinned-source", "revision": 2, "offset": 150}}
