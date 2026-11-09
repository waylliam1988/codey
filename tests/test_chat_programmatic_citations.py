"""A single completion projection supplies source URLs, without changing literals."""
from __future__ import annotations

import json

import pytest

from codey.operations.chat_citations import render_source_citations
from codey.operations.completion_gate import evaluate
from codey.operations.kernel_contracts import KernelResult, result_from_payload
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.answer_contract import citation_occurrences
from codey.utils.citation_scanner import mask_markdown_code

URL = "https://example.com/food-guide?q=鱼丸&next=(breakfast)#menu"


def session(*, kind: str = "chat", json_required: bool = False) -> TaskSession:
    current = TaskSession(TaskPolicy(frozenset({"control", "web.read"}),
        required_checks=("output_json",) if json_required else ()), task_kind=kind)
    current.record_open(URL, source_id="s1")
    current.record_open("https://example.com/not-selected", source_id="s2")
    current.record_search_result("r1", "https://example.com/official")
    return current


def render(text: str) -> str:
    return render_source_citations(text, sources={"s1": URL}, searches={"r1": "https://example.com/official"})


def test_links_come_from_the_existing_bindings_and_only_selected_sources_appear() -> None:
    state = session()
    draft = "锅边糊适合作早餐[s1]。官网入口[r1]。再次引用[s1]。"
    verdict = evaluate(state, draft)
    assert verdict.complete, verdict.followup
    assert verdict.final_text.count("[s1](<https://example.com/food-guide?") == 2
    assert "%E9%B1%BC%E4%B8%B8" in verdict.final_text
    assert "[r1](<https://example.com/official>)" in verdict.final_text
    assert "not-selected" not in verdict.final_text
    assert state.last_done_text == "" and state.source_ids["s1"] == URL
    saved = KernelResult(True, verdict.final_text, 3, "done", proof=verdict.proof)
    # The existing result codec is the sole persisted delivery; no citation schema.
    assert result_from_payload(saved.to_payload()) == saved


@pytest.mark.parametrize("draft", ["未知[s99]", "未知[r99]", "零[s0]", "范围[s1-s2]", "混合[s1, r99]"])
def test_unknown_or_invalid_references_reject_completion_without_guessing(draft: str) -> None:
    verdict = evaluate(session(), draft)
    assert not verdict.complete
    assert "source label" in verdict.followup
    assert "https://" not in verdict.final_text


@pytest.mark.parametrize("draft", ["[s1](https://wrong.invalid)", "[s1]: https://wrong.invalid", "[name][s1]",
                                  "[s1][custom]"])
def test_model_cannot_attach_its_own_destination_to_a_reserved_source_label(draft: str) -> None:
    with pytest.raises(ValueError, match="application supplies link destinations"):
        render(draft)


def test_adjacent_citations_and_prose_colons_are_valid_source_selections() -> None:
    value = render("两种入口[s1][r1]。来源[s1]: 早餐。")
    assert value.count("[s1](<") == 2
    assert value.count("[r1](<") == 1
    assert ">): 早餐。" in value


def test_search_only_link_does_not_become_a_read_source() -> None:
    state = TaskSession(TaskPolicy(frozenset({"control", "web.read"})), task_kind="chat")
    state.record_search_result("r1", URL)
    verdict = evaluate(state, "官网入口[r1]")
    assert verdict.complete, verdict.followup
    assert "[r1](<https://example.com/food-guide?" in verdict.final_text
    assert not state.source_ids
    assert not evaluate(state, "已读来源[s1]").complete


def test_comma_groups_repeated_labels_and_escaped_literals() -> None:
    value = render(r"来源[s1, r1, s1]；字面量\[s999]；图片![s999]")
    assert value.count("[s1](<") == 1 and value.count("[r1](<") == 1
    assert r"\[s999]" in value and "![s999]" in value
    assert "[s1](<" in render(r"反斜杠\\[s1]")


@pytest.mark.parametrize("draft", [
    '```python\nprint("[s999]")\n```',
    '~~~~text\n[s999]\n~~~~',
    '```text\n[s999]',
    '    print("[s999]")\n',
    '文字 `[s999]` 与 ``code `[r999]` ``。',
    '跨行 `literal\n[s999]`。',
])
def test_code_literals_do_not_become_source_selections(draft: str) -> None:
    assert render(draft) == draft


@pytest.mark.parametrize("draft", ['{"literal":"[s999]"}', '["[s999]", 17]', '"[r999]"'])
def test_json_literals_are_kept_byte_for_byte(draft: str) -> None:
    assert render(draft) == draft
    verdict = evaluate(session(json_required=True), draft)
    assert verdict.complete, verdict.followup
    assert verdict.final_text == draft and json.loads(verdict.final_text) == json.loads(draft)


def test_invalid_explicit_json_is_checked_as_json_instead_of_citation_prose() -> None:
    verdict = evaluate(session(json_required=True), '{"literal":"[s999]"')
    assert not verdict.complete and "Final answer must be JSON" in verdict.followup
    assert "Unknown source label" not in verdict.followup


def test_existing_hyperlinks_and_other_task_domains_are_not_rewritten() -> None:
    draft = "用户给的链接 [网页](https://example.org/already-given)"
    assert render(draft) == draft
    verdict = evaluate(session(kind="project"), "literal [s999]")
    assert verdict.complete and verdict.final_text == "literal [s999]"


def test_shared_code_mask_preserves_offsets_and_research_label_accounting() -> None:
    draft = "正文[e1]\n```python\n'[e99]'\n```\n继续[e2] 与 `[e98]`"
    masked = mask_markdown_code(draft)
    assert len(masked) == len(draft)
    rows = citation_occurrences(draft)
    assert [row["label"] for row in rows] == ["e1", "e2"]
    assert all(draft[row["start"]:row["end"]] == f'[{row["label"]}]' for row in rows)


def test_nested_list_prose_remains_citable_while_nested_code_and_quoted_code_stay_literal() -> None:
    draft = ("- 说明\n    早餐[s1]\n\n      print('[s999]')\n"
             "> ```python\n> print('[s999]')\n> ```\n"
             "- ```text\n  [s999]\n  ```\n"
             "同一行 ```[s999]``` 与正文[s1]。")
    rendered = render(draft)
    assert rendered.count("[s1](<") == 2
    assert rendered.count("[s999]") == 4
    research = "- 说明\n    已支持[e1]\n\n      print('[e999]')\n"
    assert [row["label"] for row in citation_occurrences(research)] == ["e1"]
