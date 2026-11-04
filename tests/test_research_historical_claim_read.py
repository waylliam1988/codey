"""Prior interpretations lead whole answers to pinned material, never fN adoption."""

import json
import subprocess
import sys
from dataclasses import replace

import pytest

from codey.knowledge.model import KnowledgeMutation as M
from codey.knowledge.model import KnowledgeObject as K
from codey.knowledge.model import KnowledgeReference as R
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore
from codey.operations.task_execution import ExecutionDelegate
from codey.research.answer_contract import answer_scope, submit_answer
from codey.research.answer_report import compile_answer_report
from codey.research.tools import ResearchTools
from tests.test_research_deterministic_completion import _run
from tests.test_research_findings import TEXT, URL, execute
from tests.test_research_long_term_loop import strict_session
from tests.test_research_whole_answer import record_submission


def seed_history():
    return [
        M(K("source", "source", TEXT, "source", provenance={"url": URL})),
        M(K("evidence", "evidence", TEXT, "observed_result", references=(R("source", 1, "from_source"),))),
        M(K("claim", "claim", "Only one writer per database.", "model_extraction",
            conditions={"database": {"state": "reported", "value": "same database"}},
            references=(R("evidence", 1, "supported_by"),))),
        M(K("inference", "inference", "A write queue might reduce contention; workload tests are needed.",
            "model_inference", references=(R("claim", 1, "depends_on"),))),
        M(K("observation", "observation", "One writer was observed in the experiment.", "observed_result")),
    ]


@pytest.mark.parametrize("kind", ["claim", "inference", "observation"])
def test_whole_prior_interpretation_requires_a_separate_pinned_material_read(tmp_path, kind):
    store = KnowledgeStore(tmp_path)
    store.repository.commit("seed", seed_history())
    store.close()
    store = KnowledgeStore(tmp_path)
    tools = ResearchTools(None, store, None, session_id="later")
    session = strict_session()
    delegate = ExecutionDelegate(session=session, research_tools=tools)

    def read(identity):
        delegate._research_scope = answer_scope(session)
        result = execute(delegate, tools, session, "knowledge_read", {"id": identity, "revision": 1})
        assert result.ok, result.model_text
        session._memory_results[identity] = result
        return result

    try:
        payload = json.loads(read(kind).model_text)
        assert payload["kind"] == kind and payload["revision"] == 1
        assert payload["interpretation_review"] == ("not_applicable" if kind == "observation" else "unreviewed")
        assert "finding_ref" not in payload and "evidence" not in payload
        assert "not citable evidence" in payload["citation_guidance"]
        assert "knowledge_read" in payload["citation_guidance"] and "[eN]" in payload["citation_guidance"]
        assert not tools.ledger.references and not tools.ledger.read_passages
        assert not answer_scope(session).aliases
        with pytest.raises(ValueError, match="explicit.*eN"):
            submit_answer(answer_scope(session), {"state": "answered", "markdown": "Historical answer [f1]."})
        with pytest.raises(ValueError, match="Unknown or unread"):
            submit_answer(answer_scope(session), {"state": "answered", "markdown": "Historical answer [e1]."})
        if kind == "observation":
            assert payload["references"] == []
            return
        if kind == "inference":
            assert payload["references"] == [{"target": "claim", "revision": 1, "relation": "depends_on"}]
            payload = json.loads(read("claim").model_text)
        assert payload["conditions"]["database"]["value"] == "same database"
        ref = payload["references"][0]
        assert ref == {"target": "evidence", "revision": 1, "relation": "supported_by"}
        material = read(ref["target"])
        assert material.canonical["read_passages"][0]["evidence_version"] == ["evidence", 1]
        assert [label for label, _ in answer_scope(session).aliases] == ["e1"]
        store.repository.commit("later", [M(replace(store.repository.read("source").value, content="New source."), 1)])
        record_submission(session, {"state": "answered", "markdown": "The original source limits writers [e1]."})
        report = compile_answer_report(session, tools.ledger, store, project="", session_id="later")
        assert report.source_versions[0]["revision"] == 1
        assert report.evidence_bindings[0]["evidence_id"] == "evidence"
        assert report.evidence_bindings[0]["excerpt"] == TEXT
        assert not report.knowledge_mutations
    finally:
        store.close()


@pytest.mark.parametrize("whole", [False, True])
@pytest.mark.parametrize("current_note", [False, True])
def test_document_note_read_uses_its_admitted_submission_contract(tmp_path, whole, current_note):
    store = KnowledgeStore(tmp_path)
    note = KnowledgeNote.create(type="fact", title="Writer history", body="Prior writer interpretation.")
    store.publish_note(note, "seed", seed_history())
    tools = ResearchTools(None, store, None, session_id="later", answer_submission=True, finding_report=True,
                          created_ids=[note.id] if current_note else [])
    session = strict_session()
    delegate = ExecutionDelegate(session=session, research_tools=tools)
    if whole:
        delegate._research_scope = answer_scope(session)
    try:
        result = execute(delegate, tools, session, "knowledge_read", {"id": note.id})
        assert result.ok, result.model_text
        if whole:
            assert "[eN]" in result.model_text and "not citable evidence" in result.model_text
            assert "obtain an fN" not in result.model_text and "[finding_ref]" not in result.model_text
        else:
            # The local finding-selection protocol still supports fN adoption.
            result = tools.knowledge_read(note.id)
            if current_note:
                assert "[finding_ref]" in result.model_text
            assert "obtain an fN" not in result.model_text
        assert not tools.ledger.read_passages
    finally:
        store.close()


@pytest.mark.parametrize("native", [False, True])
def test_cold_history_claim_to_material_to_whole_publication(tmp_path, native):
    source = ("from pathlib import Path; from tests.test_research_long_term_loop import run_longitudinal; "
              "import sys; run_longitudinal(Path(sys.argv[1]), phase='first', native=sys.argv[2]=='True')")
    first = subprocess.run([sys.executable, "-c", source, str(tmp_path), str(native)],
                           capture_output=True, timeout=60)
    assert first.returncode == 0, first.stderr.decode(errors="replace")
    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        evidence = next(hit.version for hit in store.search("writer", session_id="later")
                        if hit.version.value.kind == "evidence")
        store.repository.commit("prior-interpretations", [
            M(K("old-claim", "claim", "Only one writer per database.", "model_extraction",
                scope="personal", scope_id=store.owner_id,
                conditions={"database": {"state": "reported", "value": "same database"}},
                references=(R(evidence.value.id, evidence.revision, "supported_by"),))),
            M(K("old-inference", "inference", "A write queue may reduce contention; this needs testing.",
                "model_inference", scope="personal", scope_id=store.owner_id,
                references=(R("old-claim", 1, "depends_on"),))),
        ])
    finally:
        store.close()

    calls = []

    def factory(provider):
        def send(prompt):
            calls.append(prompt)
            turn = len(calls)
            if turn == 1:
                assert "This summary is a locator" in prompt
                assert "fN" not in prompt
                name, args = "knowledge_search", {"query": "writer", "depth": 2}
            elif turn == 2:
                assert "old-inference" in prompt
                name, args = "knowledge_read", {"id": "old-inference", "revision": 1}
            elif turn == 3:
                assert "not citable evidence" in prompt
                name, args = "knowledge_read", {"id": "old-claim", "revision": 1}
            elif turn == 4:
                assert evidence.value.id in prompt and "same database" in prompt
                name, args = "knowledge_read", {"id": evidence.value.id, "revision": evidence.revision}
            else:
                assert "[e1]" in prompt
                name, args = "submit_research_answer", {"state": "answered",
                    "markdown": "Readers may overlap a writer; only one writer writes at a time [e1]."}
            return json.dumps({"tool": name, "args": args})
        provider.send = send
        return provider

    def run(resume=False):
        return _run(tmp_path / "reuse", native=native, provider_factory=factory,
                    required_checks=strict_session().policy.required_checks, knowledge_root=tmp_path / "knowledge",
                    session_id="later", run_id="later", resume=resume)

    result = run()
    assert result.stop_reason == "done", result.summary
    assert result.turns == 5 and result.completion_proof.satisfied
    assert result.accepted_report.evidence_bindings[0]["evidence_id"] == evidence.value.id
    assert not result.accepted_report.knowledge_mutations
    assert '"fetch"' not in (tmp_path / "reuse" / "calls.jsonl").read_text()
    assert run(resume=True) == result and len(calls) == 5
    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        counts = dict(store.index._read_conn().execute("SELECT kind,COUNT(*) FROM knowledge_versions GROUP BY kind"))
        assert counts == {"source": 1, "evidence": 1, "answer": 2, "claim": 1, "inference": 1}
    finally:
        store.close()
