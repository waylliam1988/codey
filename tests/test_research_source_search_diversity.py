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
    assert len({hit.offset for hit in hits}) == len(hits)
    assert all(any(token in text[hit.offset:hit.offset + 640].lower() for token in ("checkpoint", "sqlite_busy"))
               for hit in hits)


def test_labels_remain_discoverable_when_there_is_no_body_match():
    text = "Table Of Contents\n9. Queries Return SQLITE_BUSY\n\n## Overview\nUnrelated body."
    assert any("SQLITE_BUSY" in hit.snippet for hit in search_text(text, "SQLITE_BUSY"))
    assert query_tokens("AND") == ("and",)


def test_multiple_matches_that_open_the_same_paragraph_are_not_repeated():
    text = "\n\n".join("Target " + "words " * 40 + "target " + "words " * 30 for _ in range(6))
    hits = search_text(text, "target", 6)
    assert len({hit.offset for hit in hits}) == len(hits) == 6


def test_pdf_pages_with_identical_offsets_remain_separate_hits():
    pages = {1: "Reported outcome in model A.", 2: "Reported outcome in model B."}
    assert {hit.page for hit in search_pages(pages, "outcome")} == {1, 2}


def test_repeated_common_terms_do_not_hide_another_requested_term():
    background = ("WAL mode compared with rollback journal. " + "background " * 40 + "\n\n") * 12
    target = "A connection can return SQLITE_BUSY while another connection performs recovery."
    text = background + target
    hits = search_text(text, "SQLITE_BUSY WAL mode vs rollback journal", 6)
    assert len(hits) == 6
    assert any("SQLITE_BUSY" in hit.snippet for hit in hits[:2])
    assert all(hit.snippet.replace("...", "") in " ".join(text[max(0, hit.offset - 130):].split())
               for hit in hits)


def test_pdf_query_coverage_selects_the_useful_window_on_a_later_page():
    common = "WAL mode compared with rollback journal. " + "background " * 40
    pages = {n: common for n in range(1, 8)}
    pages[8] = common + "A connection can return SQLITE_BUSY during recovery."
    hits = search_pages(pages, "SQLITE_BUSY WAL mode vs rollback journal", 3)
    assert len({hit.page for hit in hits}) == len(hits) == 3
    assert any(hit.page == 8 and "SQLITE_BUSY" in hit.snippet for hit in hits)


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


def test_source_locator_opens_the_context_its_preview_actually_shows():
    text = "前言。" * 100 + "\n\n佛跳牆是代表菜。\n\n撈化是福州特色早餐。" + "\n\n其他。" * 200
    hits = search_text(text, "福州特色早餐", limit=1)
    assert len(hits) == 1 and "佛跳牆" in hits[0].snippet
    assert "佛跳牆" in text[hits[0].offset:hits[0].offset + 1000]
