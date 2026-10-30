"""Page search must expose body passages rather than repeated TOC previews."""

from codey.research.source_search import query_tokens, search_pages, search_text


def test_common_query_words_and_repeated_toc_hits_do_not_hide_body():
    navigation = ("Table Of Contents\n2.1. Checkpointing\n3.1. Automatic Checkpoint\n"
                  "3.2. Application-Initiated Checkpoints\n9. Queries Return SQLITE_BUSY\n")
    text = (navigation + "\n## Overview\n" + "background " * 45
            + "\nA checkpoint must stop when it reaches an active reader. " + "explanation " * 40
            + "\nAn exclusive connection makes other queries return SQLITE_BUSY. " + "details " * 40)
    hits = search_text(text, "checkpoint and SQLITE_BUSY", 3)
    assert query_tokens("checkpoint and SQLITE_BUSY") == ("checkpoint", "sqlite_busy")
    assert any("active reader" in hit.snippet for hit in hits)
    assert any("exclusive connection" in hit.snippet for hit in hits)
    assert hits[0].offset > len(navigation)
    assert all(abs(a.offset - b.offset) >= 160 for i, a in enumerate(hits) for b in hits[i + 1:])
    assert all(text[hit.offset:].lower().startswith(("checkpoint", "sqlite_busy")) for hit in hits)


def test_labels_remain_discoverable_when_there_is_no_body_match():
    text = "Table Of Contents\n9. Queries Return SQLITE_BUSY\n\n## Overview\nUnrelated body."
    assert any("SQLITE_BUSY" in hit.snippet for hit in search_text(text, "SQLITE_BUSY"))
    assert query_tokens("AND") == ("and",)


def test_pdf_pages_with_identical_offsets_remain_separate_hits():
    pages = {1: "Reported outcome in model A.", 2: "Reported outcome in model B."}
    assert {hit.page for hit in search_pages(pages, "outcome")} == {1, 2}


def test_page_reading_guidance_does_not_route_source_offsets_into_tool_receipts():
    from codey.operations.kernel_prompt import _result_context
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall, ToolResult

    session = TaskSession(TaskPolicy(frozenset({"control", "web.read"})))
    session.record_open("https://example.com/paper")
    result = ToolResult(ToolCall("open_url", {"url": "https://example.com/paper"}),
                        "Source page; more text available at offset=6000", ok=True)
    session._memory_results["task_turn_effect:original"] = result
    text = _result_context(result, session)
    assert "Source offsets are not receipt offsets" in text
    assert "source_search with the opened URL" in text
    assert "If more detail is needed, use read_tool_result" not in text
