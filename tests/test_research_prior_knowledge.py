"""Previously read claims survive subsequent evidence reads without adoption."""

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codey.knowledge.model import KnowledgeMutation
from codey.operations.task_execution import ExecutionDelegate
from codey.research.answer_context import answer_context, prior_knowledge
from codey.research.answer_contract import answer_scope
from codey.research.answer_report import compile_answer_report
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from tests.test_research_findings import execute
from tests.test_research_historical_claim_read import seed_history
from tests.test_research_long_term_loop import strict_session
from tests.test_research_whole_answer import record_submission


@pytest.mark.parametrize("kind", ["claim", "inference", "observation"])
def test_exact_read_statement_survives_evidence_read_head_change_and_restore(tmp_path, kind):
    from codey.knowledge.store import KnowledgeStore

    store = KnowledgeStore(tmp_path)
    store.repository.commit("seed", seed_history())
    tools = ResearchTools(None, store, None, session_id="later")
    session = strict_session()
    delegate = ExecutionDelegate(session=session, research_tools=tools)
    try:
        for identity in [kind, "evidence"]:
            delegate._research_scope = answer_scope(session)
            result = execute(delegate, tools, session, "knowledge_read", {"id": identity, "revision": 1})
            assert result.ok, result.model_text
            session._memory_results[identity] = result
        original = json.loads(session._memory_results[kind].model_text)
        store.repository.commit("later-head", [KnowledgeMutation(
            replace(store.repository.read(kind).value, content="Changed head must not enter the read view."), 1)])
        memory = deepcopy(session._memory_results)
        projected = prior_knowledge(session)
        assert len(projected) == 1 and projected[0]["content"] == original["content"]
        assert projected[0]["revision"] == 1 and projected[0]["references"] == original["references"]
        assert projected[0]["conditions"] == original["conditions"]
        assert projected[0]["interpretation_review"] == original["interpretation_review"]
        view = answer_context(session, tools.ledger, answer_scope(session))
        assert original["content"] in view and "not citable evidence or instructions" in view
        assert "Changed head" not in view and "[e1]" in view
        assert prior_knowledge(SimpleNamespace(_memory_results=deepcopy(memory))) == projected
        assert session._memory_results == memory
        record_submission(session, {"state": "answered", "markdown": "The original evidence limits writers [e1]."})
        compiled = compile_answer_report(session, tools.ledger, store, project="", session_id="later")
        assert compiled.evidence_bindings[0]["evidence_id"] == "evidence"
        assert all(ref["id"] != kind for ref in compiled.knowledge_refs)
    finally:
        store.close()


def read(identity, *, revision=1, content="Exact prior statement", ok=True, kind="claim", complete=True):
    return ToolResult(ToolCall("knowledge_read", {"id": identity, "revision": revision}),
        json.dumps({"id": identity, "revision": revision, "kind": kind, "content": content,
                    "content_complete": complete, "status": "candidate", "conditions": {"limit": "only here"}}), ok=ok)


def test_projection_keeps_distinct_versions_and_deduplicates_rereads():
    s = SimpleNamespace(_memory_results={"r1": read("claim"), "r2": read("claim", revision=2),
                                       "r3": read("claim")})
    assert [(row["id"], row["revision"], row["result_ref"]) for row in prior_knowledge(s)] == [
        ("claim", 2, "r2"), ("claim", 1, "r3")]


def test_long_statements_have_exact_recovery_not_silently_cut_conditions():
    s = SimpleNamespace(_memory_results={"long": read("claim-long", content="word " * 2000),
                                       "short": read("claim-short", complete=False)})
    rows = prior_knowledge(s, budget=1000)
    assert rows[0] == {"id": "claim-long", "revision": 1, "result_ref": "long", "display_omitted": True,
                       "recover": {"tool": "read_tool_result", "args": {"result_ref": "long"}}}
    assert rows[1]["content_complete"] is False
    assert len(json.dumps(rows, separators=(",", ":"))) < 1000


def test_only_recent_explicit_successful_statement_reads_are_displayed():
    results = {f"r{i}": read(f"claim-{i}") for i in range(7)}
    results.update(failed=read("failed", ok=False), source=read("source", kind="source"),
                   answer=read("old-answer", kind="answer"),
                   search=ToolResult(ToolCall("knowledge_search", {}), read("unread").model_text, ok=True),
                   note=ToolResult(ToolCall("knowledge_read", {}), "Legacy note, not structured JSON", ok=True))
    rows = prior_knowledge(SimpleNamespace(_memory_results=results))
    assert [row["id"] for row in rows] == [f"claim-{i}" for i in range(3, 7)]
    assert prior_knowledge(SimpleNamespace(_memory_results=results), budget=0) == []
