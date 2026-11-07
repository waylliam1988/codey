"""Experimental selectors cannot change read authority or silently split windows."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from codey.research import answer_context as production
from tests.manual import research_window_retention_candidate as candidate
from tests.manual.research_window_retention_cases import cases


def world(texts, windows=None):
    refs = [f"p{i}" for i in range(len(texts))]
    observations = windows or [{ref} for ref in refs]
    session = SimpleNamespace(task_text="Orin R2 remote export token administrator offline", _memory_results={
        f"read-{i}": SimpleNamespace(ok=True, canonical={"read_passages": [{"ref": ref} for ref in sorted(window)]})
        for i, window in enumerate(observations)})
    scope = SimpleNamespace(aliases=tuple((f"e{i + 1}", ref) for i, ref in enumerate(refs)))
    ledger = SimpleNamespace(read_passages={ref: SimpleNamespace(excerpt=text, source=SimpleNamespace(title="Source"))
                                           for ref, text in zip(refs, texts, strict=True)})
    return session, ledger, scope


def render(_session, ledger, _scope, visible):
    return "base:" + "|".join(ledger.read_passages[ref].excerpt for _, ref in visible)


def test_fitting_context_matches_production_exactly_and_does_not_rank(monkeypatch):
    session, ledger, scope = world(["Orin rule", "catalogue"])
    monkeypatch.setattr(production, "_render_context", render)
    monkeypatch.setattr(candidate, "eviction_order", lambda *_: pytest.fail("ranking a fitting view"))
    assert candidate.answer_context(session, ledger, scope, count_tokens=len, token_budget=100) == \
        production.answer_context(session, ledger, scope, count_tokens=len, token_budget=100)


def test_overflow_keeps_early_relevant_window_without_mutating_authority(monkeypatch):
    session, ledger, scope = world(["Orin R2 export token administrator offline", "later catalogue " * 2,
                                   "unrelated archive " * 2])
    original = deepcopy(session._memory_results)
    aliases = scope.aliases
    monkeypatch.setattr(production, "_render_context", render)
    baseline = production.answer_context(session, ledger, scope, count_tokens=len, token_budget=90)
    selected = candidate.answer_context(session, ledger, scope, count_tokens=len, token_budget=90)
    assert "administrator" not in baseline and "administrator" in selected
    assert len(selected) <= 90
    assert scope.aliases == aliases and session._memory_results == original


def test_overlapping_observations_are_one_atomic_window():
    session, _ledger, scope = world(["first", "middle", "last"], [{"p0", "p1"}, {"p1", "p2"}])
    assert candidate.groups(session, scope) == [{"p0", "p1", "p2"}]


def test_duplicate_body_does_not_occupy_two_priority_slots():
    session, ledger, scope = world(["Orin export", "Orin export", "unrelated catalogue"])
    ordered = candidate.eviction_order(session, ledger, scope)
    assert ordered[0] == {"p0"}  # Older exact duplicate first; labels remain valid.
    assert len(scope.aliases) == 3


@pytest.mark.parametrize("budget", [0, -1])
def test_invalid_budget_rejected(budget):
    with pytest.raises(ValueError, match="positive"):
        candidate.answer_context(*world(["text"]), token_budget=budget)


def test_non_evidence_overflow_is_not_hidden(monkeypatch):
    monkeypatch.setattr(production, "_render_context", render)
    with pytest.raises(ValueError, match="non-evidence"):
        candidate.answer_context(*world(["text"]), count_tokens=len, token_budget=2)


def test_frozen_fixtures_have_no_scripted_reads_or_gold_in_questions():
    fixtures = cases()
    assert len([c for c in fixtures if c["split"] == "dev"]) == 6
    assert len([c for c in fixtures if c["split"] == "holdout"]) == 6
    for case in fixtures:
        assert "controlled_sequence" not in case
        assert all(any(key["quote"] in doc["text"] for doc in case["documents"]) for key in case["keys"])
        assert all(key["quote"] not in case["question"] for key in case["keys"])


@pytest.mark.parametrize("visible_elsewhere,expected", [(True, 0), (False, 1)])
def test_actual_request_removal_does_not_ignore_latest_tool_text(tmp_path, visible_elsewhere, expected):
    import json

    from tests.manual import research_window_retention_ab as ab
    from tests.manual.research_window_retention_report import inspect_run

    case = cases()[0]
    quote = case["keys"][0]["quote"]
    ab.write(tmp_path / "fixtures.json", {"cases": [case]})
    name = f"{case['id']}-901-baseline"
    directory = tmp_path / "runs" / name
    directory.mkdir(parents=True)
    view = {"overflow": True, "read_passages": [{"excerpt": quote}],
            "renders": [{"text": quote, "visible": ["e1"]}, {"text": "Reduced view", "visible": []}]}
    (directory / "retention-views.jsonl").write_text(json.dumps(view) + "\n", encoding="utf-8")
    wire = tmp_path / "wire"
    wire.mkdir()
    request = {"event": "admitted_payload", "index": 1,
               "payload": {"messages": [{"role": "user", "content": "Reduced view"},
                   {"role": "tool", "content": quote if visible_elsewhere else "Other text"}]}}
    (wire / (name + ".jsonl")).write_text(json.dumps(request) + "\n", encoding="utf-8")
    result = inspect_run(tmp_path, {"case": case["id"], "seed": 901, "arm": "baseline"})
    assert result["critical_removed_in_actual_requests"] == expected


@pytest.mark.usefixtures("public_source_dns")
def test_real_read_receipts_keep_exact_citable_snapshot_after_experimental_eviction(tmp_path, monkeypatch):
    from codey.research.answer_contract import answer_scope
    from codey.research.answer_report import compile_answer_report
    from tests.test_research_findings import execute, setup
    from tests.test_research_whole_answer import record_submission

    store, tools, session, delegate = setup(tmp_path)
    fixture = cases()[0]
    session.task_text = fixture["question"]
    # The shared deterministic DNS fixture intentionally serves example.com only.
    docs = {doc["url"].replace("example.org", "example.com"): {
        **doc, "url": doc["url"].replace("example.org", "example.com")} for doc in fixture["documents"]}
    monkeypatch.setattr(tools.search, "fetch", lambda url: {"status": "ok", "truncated": False, **docs[url]})
    try:
        # Deterministic integration setup only, never counted as model research.
        for i, url in enumerate(docs):
            session._memory_results[f"read{i}"] = execute(delegate, tools, session, "open_url", {"url": url})
        scope = answer_scope(session)
        initial_aliases = scope.aliases
        original = deepcopy(tools.ledger.read_passages)
        quote = fixture["keys"][0]["quote"]
        full = production._render_context(session, tools.ledger, scope, scope.aliases)
        floor = len(production._render_context(session, tools.ledger, scope, ()))
        demonstrated = False
        for budget in range(floor + 500, len(full), 500):
            a = production.answer_context(session, tools.ledger, scope, count_tokens=len, token_budget=budget)
            b = candidate.answer_context(session, tools.ledger, scope, count_tokens=len, token_budget=budget)
            if quote not in a and quote in b:
                demonstrated = True
                break
        assert demonstrated, "fixture must exhibit different whole-window retention"
        assert scope.aliases == initial_aliases and tools.ledger.read_passages == original
        record_submission(session, {"state": "partial", "markdown": quote + " [e1]\nOther questions remain open."})
        report = compile_answer_report(session, tools.ledger, store, project="", session_id="first")
        assert quote in report.evidence_bindings[0]["excerpt"]
        assert report.evidence_bindings[0]["passage_ref"] == dict(initial_aliases)["e1"]
    finally:
        store.close()
