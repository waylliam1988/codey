"""One source authority; contract-scoped citations never infer a read."""
import pytest

from codey.operations.completion_gate import _proof_evidence_refs, evaluate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.ledger import ResearchLedger
from codey.research.source_document import SourceDocument, SourcePage

URL = "https://example.com/observed.pdf"


def state(*, citations=True):
    policy = TaskPolicy(frozenset({"control", "web.read"}), sources_open_required=True,
                        required_checks=("research_citations",) if citations else ("research_sources_opened",))
    session, ledger = TaskSession(policy=policy), ResearchLedger()
    ledger.record_open_document(SourceDocument(URL, URL, "Report", "pdf", "application/pdf",
        "Observed page two", False, 3, (2,), (SourcePage(2, "Observed page two"),)))
    session.record_open(URL)
    return session, {"research_ledger": ledger}


def test_session_open_claim_cannot_replace_missing_committed_ledger():
    session, _ = state(citations=False)
    verdict = evaluate(session, "done")
    assert not verdict.complete and "research_ledger_missing" in verdict.followup
    assert not any(ref.startswith("source:") for ref in _proof_evidence_refs(session, {}))


def test_ledger_is_sufficient_when_session_display_aliases_are_empty():
    session, context = state(citations=False)
    session.opened_sources.clear()
    assert evaluate(session, "done", context=context).complete


@pytest.mark.parametrize("answer,reason", [
    ("Done without a citation", "research_citation_missing"),
    ("Claim [s9]", "research_citation_source_unknown"),
    ("Claim https://example.com/unread.pdf", "research_citation_source_unread"),
    ("Claim [s1 p.1]", "research_citation_page_unread"),
    ("Claim [s1 p.999]", "research_citation_page_unread"),
    ("Claim [s1 p.0]", "research_citation_page_invalid"),
    ("Claim [s1 p.3-1]", "research_citation_page_invalid"),
    ("Claim [s1 p.1-999]", "research_citation_page_invalid"),
    ("Claim [1 p.0]\n[1] Report - " + URL, "research_citation_page_invalid"),
    ("Claim [2]\n[1] Report - " + URL, "research_citation_number_unknown"),
    ("Claim [1]\n[1] Report - " + URL + "\n[1] Other - https://example.com/other", "research_citation_conflict"),
])
def test_invalid_reference_blocks_completion(answer, reason):
    session, context = state()
    verdict = evaluate(session, answer, context=context)
    assert not verdict.complete and reason in verdict.followup


@pytest.mark.parametrize("answer", ["Observed [s1 p.2]", "Observed [1 p.2]\n[1] Report - " + URL,
    '{"values":[1,2],"source":"' + URL + '"}'])
def test_valid_references_preserve_requested_final_text(answer):
    session, context = state()
    verdict = evaluate(session, answer, context=context)
    assert verdict.complete and verdict.final_text == answer


def test_search_or_alias_alone_cannot_admit_an_unread_source():
    session, context = state()
    other = "https://example.com/unread"
    session.record_open(other)
    context["research_ledger"].record_search("q", [{"url": other}])
    verdict = evaluate(session, "Claim [s2]", context=context)
    assert not verdict.complete and "research_citation_source_unread" in verdict.followup


@pytest.mark.parametrize("answer", ['{"values":[1,2],"ok":true}', "Write a story about example.org.",
    "Explain os.path.join and array[0]."])
def test_ordinary_explanations_creation_and_fixed_json_need_no_source_contract(answer):
    session = TaskSession(policy=TaskPolicy(frozenset({"control"})))
    verdict = evaluate(session, answer)
    assert verdict.complete and verdict.final_text == answer


def test_optional_citations_do_not_turn_json_arrays_into_fake_references():
    session, context = state(citations=False)
    answer = '{"values":[1,2],"ok":true}'
    assert evaluate(session, answer, context=context).final_text == answer


def test_long_source_proof_identity_uses_entire_url():
    session, context = state(citations=False)
    first = "https://example.com/?id=" + "x" * 500
    for url in (first + "a", first + "b"):
        context["research_ledger"].record_open(url, url, "Long source", "Observed")
    refs = [ref for ref in _proof_evidence_refs(session, context) if ref.startswith("source:")]
    assert len(refs) == len(set(refs)) == 3


def test_source_proof_identity_binds_observed_body_version():
    session, context = state(citations=False)
    before = _proof_evidence_refs(session, context)
    context["research_ledger"].record_open(URL, URL, "Report", "Changed body")
    assert _proof_evidence_refs(session, context) != before
