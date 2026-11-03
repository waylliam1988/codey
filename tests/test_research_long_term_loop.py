"""Strict whole research accumulates and reuses knowledge across application restarts."""

import json
import subprocess
import sys
from dataclasses import replace

import pytest

from codey.knowledge.model import KnowledgeMutation as M
from codey.knowledge.model import KnowledgeObject as K
from codey.knowledge.model import KnowledgeReference as R
from codey.knowledge.recall import KnowledgeAccess, KnowledgeRecall
from codey.knowledge.store import KnowledgeStore
from codey.operations.kernel_facts import project_result_observation
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS, TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.research.answer_contract import answer_scope
from codey.research.answer_report import compile_answer_report
from codey.research.ledger import ResearchLedger
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall
from tests.test_research_deterministic_completion import _run
from tests.test_research_findings import TEXT, URL, execute, setup
from tests.test_research_whole_answer import record_submission


def run_longitudinal(root, *, phase, native=False, boundary="", resume=False):
    knowledge = root / "knowledge"
    evidence = None
    owner = ""
    if phase == "reuse":
        store = KnowledgeStore(knowledge)
        try:
            evidence = next(hit.version for hit in store.search("writer", session_id="second")
                            if hit.version.value.kind == "evidence")
            owner = store.owner_id
        finally:
            store.close()

    def factory(provider):
        def send(prompt):
            calls = root / phase / "script.jsonl"
            rows = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
            turn = len(rows) + 1
            with calls.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"prompt": prompt}, ensure_ascii=False) + "\n")
            if phase == "first":
                name, args = (("open_url", {"url": URL}) if turn == 1 else
                              ("submit_research_answer", {"state": "answered", "markdown": "Only one writer at a time [e1]."}))
            elif turn == 1:
                name, args = "knowledge_search", {"query": "writer concurrency", "depth": 2}
            elif turn == 2:
                assert evidence.value.id in prompt
                name, args = "knowledge_read", {"id": evidence.value.id, "revision": evidence.revision}
            elif turn == 3:
                name, args = "prepare_knowledge_revision", {}
            elif turn in (4, 5):
                assert "[e1]" in prompt
                claim = K("writer-mechanism", "claim", "Writes serialize." if turn == 4 else
                          "One writer per database; concurrency alone does not measure throughput.",
                          "model_extraction", title="Serialized write mechanism", scope="personal", scope_id=owner,
                          aliases=("serialization",),
                          conditions={"database": {"state": "reported", "value": "same database"}},
                          references=(R(evidence.value.id, evidence.revision, "supported_by"),))
                changes = [{"value": claim.to_dict(), "expected_revision": turn - 4}]
                if turn == 4:
                    inference = K("shared-mechanism", "inference", "A shared write lock can constrain concurrency.",
                                  "model_inference", scope="personal", scope_id=owner,
                                  references=(R(claim.id, 1, "depends_on"),))
                    changes.append({"value": inference.to_dict(), "expected_revision": 0})
                name, args = "knowledge_commit", {"commit_id": f"accumulate-{turn}", "changes": changes}
            else:
                name, args = "submit_research_answer", {"state": "answered",
                    "markdown": "Readers may overlap a writer; writes remain serialized [e1]."}
            return json.dumps({"tool": name, "args": args})
        provider.send = send
        return provider

    return _run(root / phase, task="Explain writer concurrency." if phase == "first" else
                "Can simultaneous readers remove the single writer limit?",
                native=native, compact_planning=True, required_checks=RESEARCH_ANSWER_REQUIRED_CHECKS,
                provider_factory=factory, source_text=TEXT, knowledge_root=knowledge,
                session_id=phase, run_id=phase, boundary=boundary, boundary_turn=3, resume=resume)


@pytest.mark.parametrize("native", [False, True])
def test_research_close_rephrase_recall_read_revise_publish_and_recover(tmp_path, native):
    source = ("from pathlib import Path; from tests.test_research_long_term_loop import run_longitudinal; "
              "import sys; run_longitudinal(Path(sys.argv[1]), phase=sys.argv[2], native=sys.argv[3]=='True', "
              "boundary=sys.argv[4])")
    # Real process exits close the first application and interrupt the second
    # after its original knowledge read; no live model or network provider.
    first = subprocess.run([sys.executable, "-c", source, str(tmp_path), "first", str(native), ""],
                           capture_output=True, timeout=60)
    assert first.returncode == 0, first.stderr.decode(errors="replace")
    interrupted = subprocess.run([sys.executable, "-c", source, str(tmp_path), "reuse", str(native), "provider_request_ready"],
                                 capture_output=True, timeout=60)
    assert interrupted.returncode == 74, interrupted.stderr.decode(errors="replace")
    result = run_longitudinal(tmp_path, phase="reuse", native=native, resume=True)
    assert result.stop_reason == "done", result.summary
    assert result.turns == 6
    calls = tmp_path / "reuse" / "script.jsonl"
    before = calls.read_bytes()
    assert run_longitudinal(tmp_path, phase="reuse", native=native, resume=True) == result
    assert calls.read_bytes() == before
    assert '"fetch"' not in (tmp_path / "reuse" / "calls.jsonl").read_text()
    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        versions = store.index._read_conn().execute("SELECT kind,COUNT(*) FROM knowledge_versions GROUP BY kind").fetchall()
        assert dict(versions) == {"answer": 2, "source": 1, "evidence": 1, "claim": 2, "inference": 1}
        old = store.repository.read("writer-mechanism", 1)
        latest = store.repository.read("writer-mechanism")
        assert old.value.content == "Writes serialize." and latest.revision == 2
        assert latest.interpretation_review == "unreviewed"
        assert latest.value.conditions["database"]["value"] == "same database"
        assert store.repository.read("shared-mechanism").dependencies_changed
        hits = store.search("serialization", session_id="third", depth=2)
        assert any(hit.version.value.id == latest.value.id and hit.version.revision == 2 for hit in hits)
        assert all(hit.version.value.scope == "personal" and hit.version.value.scope_id == store.owner_id for hit in hits)
        assert not KnowledgeRecall(store.repository).search("writer", access=KnowledgeAccess(owner_id="another-owner"))
        assert not result.accepted_report.knowledge_mutations
        binding = result.accepted_report.evidence_bindings[0]
        assert binding["evidence_id"] == latest.value.references[0].target
        assert binding["evidence_revision"] == 1
    finally:
        store.close()


def strict_session():
    return TaskSession(TaskPolicy(frozenset({"control", "knowledge.read", "knowledge.write", "web.read"}),
                                  strict_research=True, required_checks=RESEARCH_ANSWER_REQUIRED_CHECKS),
                       task_kind="research", task_text="Explain the writer limit.")


@pytest.mark.parametrize("kind", ["source", "evidence"])
def test_strict_read_pins_revision_and_receipt_restores_without_store_or_network(tmp_path, kind):
    store, tools, _, delegate = setup(tmp_path)
    session = strict_session()
    delegate.session = session
    try:
        store.repository.commit("seed", [M(K("original", "source", TEXT, "source", title="Writer limits",
            status="active", provenance={"url": URL})),
            M(K("excerpt", "evidence", TEXT, "observed_result", status="active",
                references=(R("original", 1, "from_source"),)))])
        identity = "original" if kind == "source" else "excerpt"
        snapshot = build_turn_snapshot(session, native=True)
        plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("read", "knowledge_read",
            {"id": identity, "revision": 1}),)), snapshot=snapshot)
        assert not plan.protocol_error
        read = execute(delegate, tools, session, "knowledge_read", {"id": identity, "revision": 1})
        assert read.ok, read.model_text
        session._memory_results["read"] = read
        store.repository.commit("later", [M(replace(store.repository.read("original").value,
                                                   content="Later incompatible contents."), 1)])
        cold = ResearchLedger()
        project_result_observation(session, read.call, read, ok=True, research_ledger=cold)
        assert cold.read_passages == tools.ledger.read_passages
        record_submission(session, {"state": "answered", "markdown": "The original limits writers [e1]."})
        report = compile_answer_report(session, cold, store, project="", session_id="first")
        assert report.source_versions[0]["id"] == "original"
        assert report.source_versions[0]["revision"] == 1
        assert report.evidence_bindings[0]["excerpt"] == TEXT
        if kind == "evidence":
            assert not report.knowledge_mutations
            assert report.evidence_bindings[0]["evidence_id"] == "excerpt"
        assert not any(row["value"]["kind"] == "source" for row in report.knowledge_mutations)
    finally:
        store.close()


@pytest.mark.parametrize("scope,owner,allowed", [("global", "", True), ("personal", "self", True),
    ("personal", "stranger", False), ("session", "first", True), ("session", "other", False),
    ("project", "current", True), ("project", "other", False)])
def test_material_access_and_archive_scope_remain_restricted(tmp_path, scope, owner, allowed):
    from codey.research.answer_archive import publication_command

    store, tools, session, delegate = setup(tmp_path)
    tools.project = "current"
    owner = store.owner_id if owner == "self" else owner
    try:
        store.repository.commit("seed", [M(K("source", "source", TEXT, "source", scope=scope, scope_id=owner,
            provenance={"url": URL})), M(K("evidence", "evidence", TEXT, "observed_result", scope=scope, scope_id=owner,
                references=(R("source", 1, "from_source"),)))])
        read = execute(delegate, tools, session, "knowledge_read", {"id": "evidence", "revision": 1})
        assert read.ok is allowed
        assert bool(store.search("writer", project="current", session_id="first")) is allowed
        if not allowed:
            assert not read.canonical and not tools.ledger.read_passages
            return
        session._memory_results["read"] = read
        record_submission(session, {"state": "answered", "markdown": "The writer limit is explicit [e1]."})
        report = compile_answer_report(session, tools.ledger, store, project="current", session_id="first")
        command = publication_command(report, task=session.task_text, run_id="run", projection="initial",
                                      note_id="note", session_id="first", project="current", store=store)
        answer = command["mutations"][-1]["value"]
        assert (answer["scope"], answer["scope_id"]) == ((scope, owner) if scope != "global" else ("project", "current"))
        store.commit_knowledge(command["commit_id"], [M(K.from_dict(row["value"]), row["expected_revision"])
                                                     for row in command["mutations"]])
    finally:
        store.close()


def test_strict_optional_accumulation_cannot_create_a_second_answer(tmp_path):
    store, tools, _, delegate = setup(tmp_path)
    session = strict_session()
    delegate.session = session
    try:
        snapshot = build_turn_snapshot(session, native=True)
        args = {"commit_id": "bypass", "changes": [{"expected_revision": 0,
                "value": K("second-answer", "answer", "Bypass publication", "model_inference").to_dict()}]}
        plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("commit", "knowledge_commit", args),)),
                              snapshot=snapshot)
        assert plan.protocol_error
        delegate._research_scope = snapshot.research_scope
        result, ok, _ = delegate._execute_research(ToolCall("knowledge_commit", args))
        assert not ok and "publication" in result.model_text
        assert store.repository.read("second-answer") is None
    finally:
        store.close()


def test_historical_source_reads_are_bounded_and_offsets_keep_original_versions(tmp_path):
    store = KnowledgeStore(tmp_path)
    tools = ResearchTools(None, store, None, session_id="new")
    try:
        content = "Original fact. " * 2000
        store.repository.commit("long", [M(K("long-source", "source", content, "source", provenance={"url": URL}))])
        first = tools.knowledge_read("long-source", revision=1, limit=1000)
        assert first.ok and sum(row["end"] - row["start"] for row in first.read_passages) <= 1000
        second = tools.knowledge_read("long-source", revision=1, offset=20000, limit=1000)
        assert second.ok and second.read_passages[0]["start"] == 20000
        assert all(row.source_version == ("long-source", 1) for row in tools.ledger.read_passages.values())
        count = len(tools.ledger.read_passages)
        assert not tools.knowledge_read("long-source", offset=len(content)).ok
        assert len(tools.ledger.read_passages) == count
    finally:
        store.close()


def test_identical_bytes_in_two_knowledge_revisions_never_rebind_an_existing_label(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        original = K("source", "source", TEXT, "source", title="Original title", provenance={"url": URL})
        store.repository.commit("first", [M(original)])
        session._memory_results["first"] = execute(delegate, tools, session, "knowledge_read", {"id": "source", "revision": 1})
        before = answer_scope(session)
        store.repository.commit("metadata", [M(replace(original, title="Revised metadata"), 1)])
        session._memory_results["second"] = execute(delegate, tools, session, "knowledge_read", {"id": "source", "revision": 2})
        after = answer_scope(session)
        assert after.aliases[0] == before.aliases[0]
        assert len(after.aliases) == 2
        assert tools.ledger.read_passages[after.passage_refs[0]].source_version == ("source", 1)
        assert tools.ledger.read_passages[after.passage_refs[1]].source_version == ("source", 2)
        record_submission(session, {"state": "answered", "markdown": "The original source limits writers [e1]."})
        report = compile_answer_report(session, tools.ledger, store, project="", session_id="first")
        assert report.source_versions[0]["revision"] == 1
        record_submission(session, {"state": "answered", "markdown": "Both fixed source versions limit writers [e1, e2]."})
        report = compile_answer_report(session, tools.ledger, store, project="", session_id="first")
        assert [(row["id"], row["revision"]) for row in report.source_versions] == [("source", 1), ("source", 2)]
        assert "[e1] Original title" in report.text and "[e2] Revised metadata" in report.text
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_first_project_research_archives_in_the_project_and_only_that_project_recalls_it(tmp_path):
    from codey.research.answer_archive import publication_command

    store, tools, session, delegate = setup(tmp_path)
    tools.project = "current"
    try:
        session._memory_results["read"] = execute(delegate, tools, session, "open_url", {"url": URL})
        record_submission(session, {"state": "answered", "markdown": "The original source limits writers [e1]."})
        report = compile_answer_report(session, tools.ledger, store, project="current", session_id="first")
        command = publication_command(report, task=session.task_text, run_id="project-run", projection="initial",
                                      note_id="project-note", session_id="first", project="current", store=store)
        store.commit_knowledge(command["commit_id"], [M(K.from_dict(row["value"]), row["expected_revision"])
                                                     for row in command["mutations"]])
        hits = store.search("writer", project="current", session_id="second")
        assert {hit.version.value.kind for hit in hits} == {"source", "evidence", "answer"}
        assert all((hit.version.value.scope, hit.version.value.scope_id) == ("project", "current") for hit in hits)
        assert not store.search("writer", project="other", session_id="second")
    finally:
        store.close()


@pytest.mark.parametrize("forgery", ["inaccessible", "different-bytes"])
def test_compilation_rechecks_access_and_bytes_of_receipt_pins(tmp_path, forgery):
    from codey.research.passages import passage_ref, restore_passages

    store, tools, session, delegate = setup(tmp_path)
    try:
        original = K("source", "source", TEXT, "source", provenance={"url": URL})
        target = replace(original, id="forged", content=TEXT if forgery == "inaccessible" else "Different bytes.",
                         scope="project" if forgery == "inaccessible" else "global",
                         scope_id="another-project" if forgery == "inaccessible" else "")
        store.repository.commit("seed", [M(original), M(target)])
        read = execute(delegate, tools, session, "knowledge_read", {"id": "source", "revision": 1})
        row = {**read.canonical["read_passages"][0], "source_version": ["forged", 1]}
        source = next(iter(tools.ledger.read_passages.values())).source
        row["ref"] = passage_ref(source.final_url, source.text_hash, row["start"], row["end"], source_version=["forged", 1])
        cold = ResearchLedger()
        restore_passages(cold, read.canonical["research_observation"], [row])
        read.canonical["read_passages"] = [row]
        session._memory_results["read"] = read
        record_submission(session, {"state": "answered", "markdown": "The source limits writers [e1]."})
        with pytest.raises(ValueError, match="inaccessible|differs from its pinned"):
            compile_answer_report(session, cold, store, project="", session_id="first")
        assert store.repository.read("source").revision == 1
        assert store.index._read_conn().execute("SELECT COUNT(*) FROM knowledge_versions WHERE kind='answer'").fetchone()[0] == 0
    finally:
        store.close()
