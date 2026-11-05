"""Audit controls: external oracles, real memory reload, and honest wire costs."""
import json
from contextlib import closing
from types import SimpleNamespace

import pytest

from codey.knowledge.store import KnowledgeStore
from codey.operations.task_context import ProjectTaskContextBuilder
from codey.workspace.facts import ProjectFactsStore
from tests.manual.archive_coding_pipeline_evidence import compact, expand
from tests.manual.coding_pipeline_cases import BY_NAME
from tests.manual.coding_pipeline_value_ab import EvidenceProvider, grade, write_files
from tests.manual.coding_review_value_ab import seed_files


def test_evidence_archive_preserves_repeated_requests_and_unknown_usage():
    request = {"model": "local", "messages": [{"role": "user", "content": "task"}],
               "tools": [{"type": "function", "function": {"name": "done"}}]}
    batches = {"batch": {"rows": [{"requests": [request, request], "reported_input_tokens": None}],
                         "histories": [{"requests": [request]}]}}
    archive = compact(batches)
    assert expand(archive) == batches
    assert len(archive["messages"]) == len(archive["tool_catalogs"]) == len(archive["requests"]) == 1
    assert "requests" in batches["batch"]["rows"][0]


@pytest.mark.parametrize("name,flawed_score", [("discount-bug", 2), ("tag-feature", 3)])
def test_external_oracle_catches_flaws_hidden_by_visible_tests(tmp_path, name, flawed_score):
    case = BY_NAME[name]
    write_files(tmp_path, case.files)
    write_files(tmp_path, seed_files(name, False))
    flawed = grade(tmp_path, case)
    assert flawed["quality_score"] == flawed_score
    assert flawed["scope_ok"]
    write_files(tmp_path, seed_files(name, True))
    correct = grade(tmp_path, case)
    assert correct["quality_passed"] and correct["scope_ok"]
    # Scope breaches cannot be hidden behind passing behavior.
    (tmp_path / "unrequested.py").write_text("", encoding="utf-8")
    assert not grade(tmp_path, case)["scope_ok"]


def test_wire_accounting_preserves_native_and_missing_reported_tokens():
    class Provider:
        native_tools = True
        api_protocol = "openai-completions"
        request_scoped_tool_context = True

        def _observe_http_attempt(self, **kwargs):
            pass

        def bind_usage(self, connection_id, sink):
            self.sink = sink

    concrete = Provider()
    observed = EvidenceProvider(concrete)
    assert observed.native_tools and observed.request_scoped_tool_context
    assert observed.api_protocol == concrete.api_protocol
    mark = observed.mark()
    concrete._observe_http_attempt(phase="request", data=json.dumps({"messages": [
        {"role": "user", "content": "Coding current local context:"}]}).encode())
    concrete.sink(SimpleNamespace(to_payload=lambda: {"usage": {"input_tokens": None, "output_tokens": None},
                                                       "context": {"value": 123, "method": "tokenizer"}}))
    row = observed.evidence(mark)
    assert row["http_requests"] == 1
    assert row["reported_input_tokens"] is None
    assert row["reported_output_tokens"] is None
    assert row["input_context_tokens"] == 123
    assert row["coding_context_request_count"] == 1
    assert observed.evidence(observed.mark())["http_requests"] == 0


def test_empty_fact_projection_and_persisted_checks_reload(tmp_path):
    root, state = tmp_path / "project", tmp_path / "state"
    root.mkdir()
    write_files(root, BY_NAME["discount-bug"].files)

    def build(facts, store):
        return ProjectTaskContextBuilder(project_facts=facts, knowledge_store=store).build(
            project=root, task="Fix pricing", session_id="audit", run_id="ab",
            continue_task=False, provider_session_changed=False)

    baseline = build(None, None)
    with closing(KnowledgeStore(state / "vault")) as store:
        cold = build(ProjectFactsStore(state), store)
        assert cold.verified_facts == baseline.verified_facts == ""
        assert cold.research_context == baseline.research_context == ""
        assert cold.verification_candidates == baseline.verification_candidates
    # This deterministic test records a check as setup; the live harness only
    # seeds from actual headless success and never calls record_success itself.
    assert ProjectFactsStore(state).record_success(root, ".", "python -m pytest")
    with closing(KnowledgeStore(state / "vault")) as store:
        warm = build(ProjectFactsStore(state), store)
        assert "python -m pytest" in warm.verified_facts
        assert any(candidate.previously_passed for candidate in warm.verification_candidates)
