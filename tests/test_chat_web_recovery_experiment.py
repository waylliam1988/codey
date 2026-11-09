"""The experiment must observe production facts without granting test shortcuts."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from codey.operations import task_guidance
from codey.operations.kernel_progress import KernelProgress
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.web_sources import WebSourceProvider
from codey.runtime.core.models import ToolCall, ToolResult
from tools.chat_web_experiment_candidates import (
    READING_GUIDANCE,
    RECOVERY_GUIDANCE,
    REPEAT_GUIDANCE,
    candidate_variant,
    citation_identity_problem,
    cross_query_feedback,
)
from tools.chat_web_recovery_experiment import CASES, aggregate, analyze, fixed_sources, provider_usage


def chat() -> TaskSession:
    return TaskSession(TaskPolicy(frozenset({"control", "web.read"})), task_kind="chat")


@pytest.mark.parametrize("ok", [False, True])
def test_cross_query_feedback_restores_and_preserves_stopping_budget(ok: bool) -> None:
    session = chat()
    progress = KernelProgress()
    for n, query in enumerate(("long query", "another query")):
        progress.observe([ToolResult(ToolCall("web_search", {"query": query}), "same results", ok=ok)],
                         session, batch_id=f"batch-{n}")
    # Production exact-call feedback misses this case; the candidate uses the
    # existing result fingerprint, including failure status, without resetting it.
    assert progress.repeat_feedback() == ""
    restored = KernelProgress(snapshot=progress.snapshot())
    assert cross_query_feedback(restored) == REPEAT_GUIDANCE
    assert restored.snapshot() == progress.snapshot()
    with candidate_variant("repeat"):
        assert restored.repeat_feedback() == REPEAT_GUIDANCE
    assert restored.repeat_feedback() == ""
    for n in range(5):
        progress.observe([ToolResult(ToolCall("web_search", {"query": f"query-{n}"}), "same results", ok=ok)],
                         session, batch_id=f"later-{n}")
    assert progress.stopped
    assert cross_query_feedback(progress) == ""


@pytest.mark.parametrize("change", ["new_output", "other_tool", "changed", "epoch", "same_query"])
def test_cross_query_feedback_does_not_misreport_other_observations(change: str) -> None:
    session = chat()
    progress = KernelProgress()
    first = ToolResult(ToolCall("web_search", {"query": "one"}), "same results", ok=True)
    second = ToolResult(ToolCall("web_search", {"query": "two"}), "same results", ok=True)
    if change == "new_output":
        second = replace(second, model_text="new results")
    elif change == "other_tool":
        second = replace(second, call=ToolCall("read_file", {"path": "two"}))
    elif change == "changed":
        second = replace(second, audit={"changed": True})
    elif change == "same_query":
        second = replace(second, call=first.call)
    progress.observe([first], session, batch_id="one")
    if change == "epoch":
        session.edited_files["app.py"] = 1
    progress.observe([second], session, batch_id="two")
    assert cross_query_feedback(progress) == ""


def test_guidance_variants_are_scoped_and_reverted() -> None:
    original = task_guidance.task_guidance_for_policy
    policy = chat().policy
    for variant, included, excluded in (("recovery", RECOVERY_GUIDANCE, READING_GUIDANCE),
                                         ("reading", READING_GUIDANCE, RECOVERY_GUIDANCE)):
        from tools.chat_web_recovery_experiment import parse_variant

        with candidate_variant(parse_variant(variant)):
            text = task_guidance.task_guidance_for_policy(policy, task_kind="chat")
            assert included in text and excluded not in text
            assert included not in task_guidance.task_guidance_for_policy(policy, task_kind="project")
            disabled = replace(policy, grants=frozenset({"control"}))
            assert included not in task_guidance.task_guidance_for_policy(disabled, task_kind="chat")
        assert task_guidance.task_guidance_for_policy is original


def test_citation_experiment_distinguishes_identity_from_reading_obligation() -> None:
    session = chat()
    assert citation_identity_problem(session, "解释示例 [r1]") == ""
    session.search_results["r1"] = "https://example.com/official"
    assert "internal" in citation_identity_problem(session, "参考 [r1, r2]")
    assert citation_identity_problem(session, "官网链接：https://example.com/official") == ""
    assert citation_identity_problem(session, "这是一般聊天，无需引用。") == ""
    assert "absent" in citation_identity_problem(session, "来源：https://invented.example/article")
    session.required_source_urls = frozenset({"https://requested.example/article"})
    assert citation_identity_problem(session, "未能读取：https://requested.example/article") == ""


def test_rejected_identity_candidate_exposes_italic_url_boundary_failure() -> None:
    # Preserve the live failure as an experimental counterexample, not a
    # production requirement: this valid citation must not justify promotion.
    session = chat()
    session.search_results["r1"] = "https://example.com/source"
    assert citation_identity_problem(session, "来源：(https://example.com/source)") == ""
    problem = citation_identity_problem(session, "*来源：(https://example.com/source)*")
    assert "absent" in problem and "https://example.com/source)*" in problem


def test_fixed_search_has_no_answer_hint_and_both_variants_receive_same_source(tmp_path: Path) -> None:
    case = CASES["food_recovery"]
    with fixed_sources(case, tmp_path):
        provider = WebSourceProvider()
        assert "汉语文字" in str(provider.search("福州美食 推荐 吃什么 福州小吃")[0]["title"])
        hits = provider.search("福州美食")
        assert hits[0]["url"] == case.url
        assert all(term not in str(hits[0]["snippet"]) for term in case.expected)
        assert provider.fetch(case.url)["text"] == case.body


def test_usage_deduplicates_the_two_observers(tmp_path: Path) -> None:
    payload = {"id": "response-1", "usage": {"prompt_tokens": 20, "completion_tokens": 4}}
    rows = [{"type": "response", "payload": payload}] * 2
    (tmp_path / "provider.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    assert provider_usage(tmp_path) == {"generations": 1, "prompt_tokens": 20, "completion_tokens": 4}


def test_aggregate_keeps_failed_trials_in_the_denominator() -> None:
    report = aggregate([
        {"case": "example", "variant": "reading", "structural_ok": True, "controlled_evidence_ok": True,
         "controlled_expected_terms_in_body": True, "turns": 4, "usage": {"prompt_tokens": 40}},
        {"case": "example", "variant": "reading", "error": "worker failed", "structural_ok": False},
    ])
    assert report["groups"] == [{"case": "example", "variant": "reading", "trials": 2,
        "structural_passes": 1, "controlled_evidence_passes": 1, "relevant_body_deliveries": 1,
        "internal_reference_failures": 0, "median_turns": 4, "median_prompt_tokens": 40}]
    assert report["manual_answer_review_required"] is True


def test_outline_terms_and_success_events_do_not_prove_relevant_body_delivery(tmp_path: Path) -> None:
    case = CASES["food_citations"]
    events = [{"type": "tool", "tool_name": "open_url", "ok": True},
              {"type": "task_done", "stop_reason": "done", "summary": "鱼丸、肉燕、锅边糊 " + case.url}]
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(row) for row in events), encoding="utf-8")
    content = ("[result: open_url]\nURL: " + case.url + "\n交通介绍\n"
               "Source section locators (labels only):\n鱼丸、肉燕、锅边糊")
    row = {"type": "request", "payload": {"messages": [{"role": "user", "content": content}]}}
    (tmp_path / "provider.jsonl").write_text(json.dumps(row), encoding="utf-8")
    report = analyze(tmp_path, case, "controlled")
    assert report["structural_ok"] is True
    assert report["controlled_expected_terms_in_body"] is False
    assert report["controlled_evidence_ok"] is False
    assert report["answer_review_required"] is True
