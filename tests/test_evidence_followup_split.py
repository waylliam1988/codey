"""Characterization tests for EvidenceFollowupController.execute_tool_call split.

Pure-extraction guard: pins every dispatch branch so the refactor can move
code without changing conditions, call semantics, or return values.
Uses a fake tool facade (execute_tool_call only touches
allowed_urls + tools.knowledge_write).
"""

from __future__ import annotations

from codey.research.evidence_followup import EvidenceFollowupController


class _FakeTools:
    def __init__(self, reply: str = "saved fact note id=n1 at fact/n1.md") -> None:
        self.reply = reply
        self.seen: list[dict] = []

    def knowledge_write(self, args: dict) -> str:
        self.seen.append(args)
        return self.reply


def _controller(urls: list[str] | None = None, reply: str | None = None) -> EvidenceFollowupController:
    allowed = urls if urls is not None else ["https://example.com/fresh"]
    tools = _FakeTools() if reply is None else _FakeTools(reply)
    return EvidenceFollowupController(tools, allowed)  # type: ignore[arg-type]


def _valid_args(url: str = "https://example.com/fresh") -> dict:
    return {
        "type": "fact",
        "title": "T",
        "body": "B",
        "sources": [url],
        "evidence": [{"source_url": url, "excerpt": "excerpt text here"}],
    }


def test_forbidden_tool_blocked() -> None:
    c = _controller()
    assert "forbidden" in c.execute_tool_call("web_search", {"query": "x"})
    assert "forbidden" in c.execute_tool_call("done", {})
    assert "forbidden" in c.execute_tool_call("", {})
    assert "forbidden" in c.execute_tool_call("open_url", {})


def test_tool_name_case_insensitive() -> None:
    c = _controller()
    assert c.execute_tool_call("Knowledge_Write", _valid_args()).startswith("saved")


def test_extra_keys_rejected() -> None:
    c = _controller()
    args = _valid_args()
    args["tags"] = ["research"]
    res = c.execute_tool_call("knowledge_write", args)
    assert "accepts only type/title/body/sources/evidence" in res
    assert "tags" in res


def test_missing_and_bad_type_rejected() -> None:
    c = _controller()
    no_type = _valid_args()
    del no_type["type"]
    assert "requires explicit type='fact'" in c.execute_tool_call("knowledge_write", no_type)
    empty_type = _valid_args()
    empty_type["type"] = "  "
    assert "requires explicit type='fact'" in c.execute_tool_call("knowledge_write", empty_type)
    bad_type = _valid_args()
    bad_type["type"] = "concept"
    assert "requires type='fact', got 'concept'" in c.execute_tool_call("knowledge_write", bad_type)


def test_type_case_insensitive() -> None:
    c = _controller()
    args = _valid_args()
    args["type"] = "Fact"
    assert c.execute_tool_call("knowledge_write", args).startswith("saved")


def test_sources_must_be_nonempty_list() -> None:
    c = _controller()
    for bad in ("https://example.com/fresh", None, {"u": 1}):
        args = _valid_args()
        args["sources"] = bad  # type: ignore[assignment]
        assert "sources to be a non-empty list" in c.execute_tool_call("knowledge_write", args)
    args = _valid_args()
    args["sources"] = []
    assert "sources to be a non-empty list" in c.execute_tool_call("knowledge_write", args)
    args = _valid_args()
    args["sources"] = ["   "]
    assert "sources to be a non-empty list" in c.execute_tool_call("knowledge_write", args)


def test_source_internal_id_and_whitelist() -> None:
    c = _controller()
    args = _valid_args()
    args["sources"] = ["s1"]
    assert "Internal IDs like s1/s2" in c.execute_tool_call("knowledge_write", args)
    args = _valid_args()
    args["sources"] = ["https://example.com/other"]
    assert "not in the allowed fresh material whitelist" in c.execute_tool_call("knowledge_write", args)


def test_evidence_must_be_nonempty_list() -> None:
    c = _controller()
    for bad in (None, [], {}, "", {"source_url": "https://example.com/fresh"}):
        args = _valid_args()
        args["evidence"] = bad  # type: ignore[assignment]
        assert "evidence to be a non-empty list" in c.execute_tool_call("knowledge_write", args)


def test_evidence_item_shape() -> None:
    c = _controller()
    args = _valid_args()
    args["evidence"] = ["not-a-dict"]
    assert "must be a JSON object" in c.execute_tool_call("knowledge_write", args)
    args = _valid_args()
    args["evidence"] = [{"excerpt": "x"}]
    assert "requires explicit source_url" in c.execute_tool_call("knowledge_write", args)
    args = _valid_args()
    args["evidence"] = [{"source": "https://example.com/fresh", "excerpt": "x"}]
    assert "'source' alias is not accepted" in c.execute_tool_call("knowledge_write", args)
    args = _valid_args()
    args["evidence"] = [{"source_url": "  ", "excerpt": "x"}]
    assert "requires explicit source_url" in c.execute_tool_call("knowledge_write", args)


def test_evidence_source_checks() -> None:
    c = _controller()
    args = _valid_args()
    args["evidence"] = [{"source_url": "s2", "excerpt": "x"}]
    assert "Internal IDs like s1/s2" in c.execute_tool_call("knowledge_write", args)
    args = _valid_args()
    args["evidence"] = [{"source_url": "https://example.com/other", "excerpt": "x"}]
    assert "not in the allowed fresh material whitelist" in c.execute_tool_call("knowledge_write", args)


def test_evidence_source_must_be_in_sources() -> None:
    c = _controller(["https://example.com/fresh", "https://example.com/fresh2"])
    args = _valid_args()
    args["sources"] = ["https://example.com/fresh"]
    args["evidence"] = [{"source_url": "https://example.com/fresh2", "excerpt": "x"}]
    assert "must be declared in the note's 'sources' list" in c.execute_tool_call("knowledge_write", args)


def test_evidence_excerpt_required_and_quote_alias() -> None:
    c = _controller()
    args = _valid_args()
    args["evidence"] = [{"source_url": "https://example.com/fresh", "excerpt": "  "}]
    assert "requires a non-empty excerpt" in c.execute_tool_call("knowledge_write", args)
    # Current tolerance: `quote` is accepted as excerpt alias (pinned, not endorsed).
    args = _valid_args()
    args["evidence"] = [{"source_url": "https://example.com/fresh", "quote": "quoted text"}]
    assert c.execute_tool_call("knowledge_write", args).startswith("saved")


def test_success_delegates_verbatim() -> None:
    c = _controller(reply="saved fact note id=n9 at fact/n9.md")
    args = _valid_args()
    assert c.execute_tool_call("knowledge_write", args) == "saved fact note id=n9 at fact/n9.md"
