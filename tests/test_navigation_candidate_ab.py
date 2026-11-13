"""Controlled replies over real Chromium; no live-model calls in pytest."""
from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from codey.automation.browser_session import BrowserSession, _browser_call
from codey.research.browser_search import BrowserSearchProvider
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object
from tools.completion_explanation_ab import Target
from tools.navigation_candidate_ab import (
    Arm,
    Candidates,
    current_links,
    fixtures,
    run_arm,
    score,
    source_fixture,
)

TARGET = Target("http://127.0.0.1:5001/v1", "fixed-only", 123456789, 10)
pytestmark = pytest.mark.usefixtures("public_source_dns")


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("arm", ["A", "B"])
def test_real_selection_navigation_read_and_delivery(tmp_path: Path, index: int, arm: Arm) -> None:
    case, expected = fixtures()[index]
    stages: list[str] = []

    def fixed(directory: Path, target: Target, messages: list[dict[str, object]], schema: JsonObject) -> JsonObject:
        stages.append(directory.name)
        payload = parse_json_object(str(messages[-1]["content"]))
        assert target == TARGET
        if directory.name == "query":
            assert "http" not in str(payload)
            return {"query": case.question}
        if directory.name == "selection":
            rows = payload["candidates"]
            assert isinstance(rows, list) and len(rows) >= 3
            selected = next(decode_json_object(row) for row in rows if decode_json_object(row)["url"] == expected.url)
            output: JsonObject = {"candidate_id": selected["candidate_id"], "reason": "与用户对象及所问详情一致"}
            if arm == "A":
                output["url"] = selected["url"]
            return output
        assert directory.name == "answer"
        read = decode_json_object(payload["read"])
        assert read["status"] == "ok" and read["url"] == expected.url
        assert all(fact in str(read["text"]) for fact in expected.facts)
        return {"answer": "，".join(expected.facts), "missing": ""}

    result = run_arm(tmp_path / "run", case, expected, arm, TARGET, reply=fixed)
    assert result["passed"], result
    assert stages == (["query", "selection", "answer"] if case.group == "search" else ["selection", "answer"])
    assert result["model_calls"] == 0
    saved = parse_json_object((tmp_path / "run" / "navigation.json").read_text(encoding="utf-8"))
    assert saved["requested_url"] == expected.url
    network = [parse_json_object(line) for line in (tmp_path / "run" / "network.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row["url"] == expected.url and row["status"] == 200 for row in network)


@pytest.mark.parametrize("arm", ["A", "B"])
def test_fixed_url_typo_changes_only_address_transport(tmp_path: Path, arm: Arm) -> None:
    case, expected = fixtures()[0]

    def fixed(directory: Path, target: Target, messages: list[dict[str, object]], schema: JsonObject) -> JsonObject:
        payload = parse_json_object(str(messages[-1]["content"]))
        if directory.name == "query":
            return {"query": "榕城检验站 2026 秋检福州鱼丸"}
        if directory.name == "selection":
            rows = payload["candidates"]
            assert isinstance(rows, list)
            selected = next(decode_json_object(row) for row in rows if decode_json_object(row)["url"] == expected.url)
            output: JsonObject = {"candidate_id": selected["candidate_id"], "reason": "正确候选"}
            if arm == "A":
                output["url"] = expected.url.replace("autumn-2026", "autumn_2026")
            return output
        return {"answer": "，".join(expected.facts), "missing": ""}

    result = run_arm(tmp_path / "run", case, expected, arm, TARGET, reply=fixed)
    assert result["target_selected"] is True
    assert result["original_url_passed"] is (arm == "B")
    assert result["facts_read"] is (arm == "B")
    assert result["passed"] is (arm == "B")
    # A's deliberately guessed final facts must not compensate for a failed read.
    assert result["facts_delivered"] is True


@contextmanager
def detail(tmp_path: Path) -> Generator[tuple[BrowserSession, Candidates]]:
    case, _ = fixtures()[2]
    browser = BrowserSearchProvider(engine="bing", headless=True, profile_dir=tmp_path / "profile")
    session = BrowserSession(browser)
    try:
        with source_fixture(browser, case, tmp_path):
            assert session.navigate(case.entry).ok
            yield session, Candidates.observe("detail", current_links(session), session.document_identity())
    finally:
        browser.close()


@pytest.mark.parametrize("mutation", ["reload", "replace_href"])
def test_detail_reference_rejected_after_document_or_link_change(tmp_path: Path, mutation: str) -> None:
    with detail(tmp_path) as (session, candidates):
        if mutation == "reload":
            assert session.navigate(fixtures()[2][0].entry).ok
        else:
            def mutate() -> object:
                raw: object = session._page_on_browser_thread().evaluate("document.querySelector('a').href='/changed-detail'")
                return raw

            _browser_call(mutate)
        with pytest.raises(ValueError, match="Stale|changed"):
            candidates.resolve({"candidate_id": candidates.rows[0]["candidate_id"], "reason": "详情"}, "B", session)


def test_wrong_candidate_is_opened_exactly_but_does_not_pass(tmp_path: Path) -> None:
    _, expected = fixtures()[2]
    with detail(tmp_path) as (session, candidates):
        wrong = candidates.rows[0]
        url, selected = candidates.resolve({"candidate_id": wrong["candidate_id"], "reason": "错误选择"}, "B", session)
        assert url == wrong["url"] and url != expected.url
        navigation = session.navigate(url)
        read = decode_json_object(session.capture())
        outcome: JsonObject = {"selected": selected, "requested_url": url, "navigation_ok": navigation.ok,
            "navigation": navigation.observation, "read": read, "answer": {"answer": "2026-11-16，0099012", "missing": ""}}
        result = score(outcome, expected)
        assert result["original_url_passed"] is True
        assert result["target_selected"] is False and result["passed"] is False


@pytest.mark.parametrize("reply", [
    {"candidate_id": "invented", "reason": "guess"},
    {"candidate_id": "first", "reason": "guess", "url": "https://example.com/copied"},
])
def test_candidate_rejects_unobserved_id_or_copied_url(tmp_path: Path, reply: JsonObject) -> None:
    with detail(tmp_path) as (session, candidates), pytest.raises(ValueError):
        candidates.resolve(reply, "B", session)


def test_address_characters_and_observation_scope_are_preserved(tmp_path: Path) -> None:
    url = "https://EXAMPLE.COM/A-b_C?x=A%2FB&n=0042#Part-1"
    first = Candidates.observe("search", [{"title": "same", "url": url, "snippet": "one"}])
    second = Candidates.observe("search", [{"title": "same", "url": url, "snippet": "two"}])
    browser = BrowserSearchProvider(engine="bing", headless=True)
    session = BrowserSession(browser)
    try:
        selected = {"candidate_id": first.rows[0]["candidate_id"], "reason": "已观察来源"}
        assert first.resolve(selected, "B", session)[0] == url
        assert browser._session is None  # resolving a search choice does not navigate
        with pytest.raises(ValueError, match="Unknown"):
            second.resolve(selected, "B", session)
    finally:
        browser.close()


@pytest.mark.parametrize("missing", ["", "none", "无"])
def test_scoring_requires_a_complete_correct_source_and_every_requested_fact(missing: str) -> None:
    _, expected = fixtures()[2]
    outcome: JsonObject = {"selected": {"url": expected.url}, "requested_url": expected.url,
        "navigation_ok": True, "navigation": {"url": expected.url, "http_status": 200},
        "read": {"status": "ok", "truncated": False, "url": expected.url, "text": "2026-11-18，0042091"},
        "answer": {"answer": "2026-11-18，0042091", "missing": missing}}
    assert score(outcome, expected)["passed"] is True
    for pointer, value in (("truncated", True), ("url", "https://example.com/other"), ("text", "2026-11-18")):
        changed = decode_json_object(outcome)
        read = decode_json_object(changed["read"])
        read[pointer] = value
        changed["read"] = read
        assert score(changed, expected)["passed"] is False
    for text in ("2026-11-18", "2026-11-18，42091", "2026-11-18，0042091，0099012"):
        changed = decode_json_object(outcome)
        changed["answer"] = {"answer": text, "missing": missing}
        assert score(changed, expected)["passed"] is False


def test_fixture_questions_contain_no_urls_and_gold_is_only_used_after_execution() -> None:
    for case, expected in fixtures():
        assert "http" not in case.question
        assert expected.url in dict(case.documents)
        assert not any(fact in case.question for fact in expected.facts)
    # Live model schemas do not enumerate the correct candidate or expected facts.
    from tools.navigation_candidate_ab import schema

    assert "enum" not in json.dumps(schema(("candidate_id", "reason")))
