"""Isolated fault ablations; no production edits or alternate runtime.

The disabled arm deliberately loses a guarantee. Passing these tests means
the experiment observed that loss, not that the disabled arm is acceptable.
"""

import copy
import io
import json
import os
import threading
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from codey.knowledge.model import KnowledgeMutation as M
from codey.knowledge.model import KnowledgeObject as K
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.recall import KnowledgeAccess
from codey.knowledge.store import KnowledgeStore
from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS
from codey.research import answer_contract, knowledge_updates, source_download
from codey.research.answer_archive import publication_command
from codey.research.answer_contract import AnswerScope, submit_answer, validate_submission
from codey.research.answer_report import compile_answer_report
from codey.research.ledger import ResearchLedger
from codey.research.source_gateway import ResearchSourceGateway
from codey.research.tools import ResearchTools
from codey.runtime.core import cancellation
from tests.test_research_deterministic_completion import _run
from tests.test_research_findings import TEXT, URL, Search, execute, setup
from tests.test_research_first_pass_knowledge import answer_args
from tests.test_research_whole_answer import record_submission
from tests.test_research_whole_kernel import run_whole


def record(name, original, disabled, *, boundary, limitation=""):
    target = os.environ.get("RESEARCH_FAULT_ARTIFACT_DIR")
    if target:
        directory = Path(target)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.json").write_text(json.dumps({
            "name": name, "original": original, "disabled": disabled,
            "boundary": boundary, "limitation": limitation,
            "kind": "controlled_fault_no_model_inference",
        }, ensure_ascii=False, indent=2), encoding="utf-8")


def test_citation_parser_fault(monkeypatch):
    scope = AnswerScope("a" * 64, (), ("attempt",))
    args = {"state": "unresolved", "markdown": "The claimed source does not exist [e99]."}
    with pytest.raises(ValueError, match="unread"):
        submit_answer(scope, args)
    monkeypatch.setattr(answer_contract, "citation_occurrences", lambda text: [])
    result = submit_answer(scope, args)
    assert "[e99]" in result["markdown"] and not result["citations"]
    record("citation_parser", "reject_unknown_e99", "accept_unknown_e99_as_uncited_text",
           boundary="submit_answer", limitation="Downstream publication validation remains independently active.")


def test_dependency_cycle_fault(monkeypatch):
    rows = [{"key": key, "kind": "inference", "content": "A candidate inference with a premise.",
             "references": [target]} for key, target in (("k1", "k2"), ("k2", "k1"))]
    with pytest.raises(ValueError, match="cyclic"):
        knowledge_updates.normalize_updates(rows, set())
    monkeypatch.setattr(knowledge_updates, "_check_cycles", lambda rows: None)
    assert len(knowledge_updates.normalize_updates(rows, set())) == 2
    record("dependency_cycles", "reject_cycle", "normalization_accepts_cycle",
           boundary="normalize_updates", limitation="Repository cycle validation is still present; this is one layer.")


def test_frozen_submission_fault():
    task = "Explain the evidence boundary."
    scope = AnswerScope(answer_contract.digest(task), (("e1", "p" + "b" * 12),), ("read",))
    raw = submit_answer(scope, {"state": "answered", "markdown": "Only one writer may write [e1]."})
    assert validate_submission(raw, task) == raw
    changed = copy.deepcopy(raw)
    changed["markdown"] = "Unlimited writers may write [e1]."
    with pytest.raises(ValueError):
        validate_submission(changed, task)
    assert (lambda value, task: value)(changed, task)["markdown"] != raw["markdown"]
    record("frozen_submission", "reject_changed_body", "skipping_validator_returns_changed_body",
           boundary="validate_submission", limitation="Boundary bypass only, not a successful corrupt kernel publication.")


def test_exact_revision_fault(tmp_path, monkeypatch):
    with closing(KnowledgeStore(tmp_path)) as store:
        source = K("source", "source", TEXT, "source", provenance={"url": URL})
        store.repository.commit("v1", [M(source)])
        store.repository.commit("v2", [M(replace(source, content="Incompatible source head, unlimited writers."), 1)])
        tools = ResearchTools(None, store, None, session_id="later")
        original = tools.knowledge_read("source", revision=1, whole_answer=True)
        assert original.ok and TEXT in original.model_text
        read = store.repository.read
        monkeypatch.setattr(store.repository, "read", lambda identity, revision=None: read(identity))
        tools = ResearchTools(None, store, None, session_id="later")
        disabled = tools.knowledge_read("source", revision=1, whole_answer=True)
        assert disabled.ok and "Incompatible source head" in disabled.model_text and TEXT not in disabled.model_text
    record("exact_revision", "requested_v1_bytes", "v2_head_bytes_substituted", boundary="knowledge_read")


def test_scope_fault(tmp_path, monkeypatch):
    with closing(KnowledgeStore(tmp_path)) as store:
        value = K("private", "observation", "Private unrelated owner's observation.", "observed_result",
                  scope="personal", scope_id="other-owner")
        store.repository.commit("seed", [M(value)])
        tools = ResearchTools(None, store, None, session_id="later")
        assert not tools.knowledge_read("private", whole_answer=True).ok
        monkeypatch.setattr(KnowledgeAccess, "allows", lambda *args: True)
        leaked = tools.knowledge_read("private", whole_answer=True)
        assert leaked.ok and value.content in leaked.model_text
    record("scope", "deny_other_owner", "other_owner_content_exposed", boundary="knowledge_read")


@pytest.mark.usefixtures("public_source_dns")
def test_atomic_publication_fault(tmp_path, monkeypatch):
    counts = []
    for disabled in (False, True):
        store, tools, session, delegate = setup(tmp_path / str(disabled))
        try:
            session._memory_results["read"] = execute(delegate, tools, session, "open_url", {"url": URL})
            record_submission(session, answer_args())
            report = compile_answer_report(session, tools.ledger, store, project="", session_id="first")
            command = publication_command(report, task=session.task_text, run_id="first", projection="p",
                                          note_id="first-note", session_id="first", project="", store=store)
            mutations = [M(K.from_dict(row["value"]), row["expected_revision"]) for row in command["mutations"]]

            def fail(*args, **kwargs):
                raise OSError("controlled display failure after knowledge writes")

            @contextmanager
            def no_transaction(index=store.index):
                yield index._conn

            with monkeypatch.context() as patcher:
                patcher.setattr(store, "write_note", fail)
                if disabled:
                    store.index._conn.isolation_level = None
                    patcher.setattr(store.index, "transaction", no_transaction)
                with pytest.raises(OSError, match="controlled display"):
                    store.publish_note(KnowledgeNote(**command["note"]), command["commit_id"], mutations)
            counts.append(store.index._read_conn().execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0])
            assert store.read_note("first-note") is None
        finally:
            store.close()
    assert counts[0] == 0 and counts[1] == 5
    record("atomic_publication", {"orphan_versions": counts[0]}, {"orphan_versions": counts[1]},
           boundary="original_publish_note_with_failed_display_write")


def test_commit_receipt_fault(tmp_path):
    with closing(KnowledgeStore(tmp_path)) as store:
        mutations = [M(K("observation", "observation", "A controlled repeatable observation.", "observed_result"))]
        first = store.repository.commit("same", mutations)
        assert store.repository.commit("same", mutations) == first
        with store.index.transaction() as conn:
            conn.execute("DELETE FROM knowledge_commits WHERE id=?", ("same",))
        with pytest.raises(ValueError):
            store.repository.commit("same", mutations)
        assert store.repository.read("observation").revision == 1
    record("commit_receipt", "replay_returns_same_result", "replay_conflicts_no_duplicate_version",
           boundary="repository.commit", limitation="Revision conflict independently prevents duplicate versions.")


@pytest.mark.usefixtures("public_source_dns")
def test_published_projection_fault(tmp_path, monkeypatch):
    result = run_whole(tmp_path)
    assert result.stop_reason == "done"
    with closing(KnowledgeStore(tmp_path / "knowledge")) as store:
        row = store.index._read_conn().execute("SELECT id FROM notes WHERE type='synthesis'").fetchone()
        note = store.read_note(row["id"])
        changed = replace(note, body="An unrelated body conflicting with the settled answer.")
        with pytest.raises(ValueError):
            store.write_note(changed)
        monkeypatch.setattr(store, "_answer_projection_refs", lambda note: [])
        store.write_note(changed)
        assert store.read_note(note.id).body == changed.body
        answers = store.index._read_conn().execute("SELECT payload FROM knowledge_versions WHERE kind='answer'").fetchall()
        assert len(answers) == 1 and json.loads(answers[0][0])["content"] != changed.body
    record("published_body_lock", "reject_projection_body_edit", "note_diverges_from_authoritative_answer",
           boundary="KnowledgeStore.write_note")


@pytest.mark.usefixtures("public_source_dns")
def test_cancellation_fault(monkeypatch):
    fetched = []
    search = Search()
    fetch = search.fetch
    search.fetch = lambda url: (fetched.append(url), fetch(url))[1]
    gateway = ResearchSourceGateway(search, ResearchLedger())
    event = threading.Event()
    event.set()
    with cancellation.scope(event):
        with pytest.raises(cancellation.TaskCancelled):
            gateway.open(URL)
        assert fetched == []
        with monkeypatch.context() as patcher:
            patcher.setattr(cancellation, "check", lambda: None)
            assert gateway.open(URL).status == "ok"
    assert fetched == [URL]
    record("cancellation", {"fetches_after_cancel": 0}, {"fetches_after_cancel": 1}, boundary="source_gateway.open")


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("repeated", [False, True])
def test_kernel_work_bound_fault(tmp_path, monkeypatch, repeated):
    counts, reasons = [], []
    for disabled in (False, True):
        calls = []

        def factory(provider, calls=calls):
            def send(prompt):
                calls.append(prompt)
                return json.dumps({"tool": "open_url", "args": {"url": URL if repeated else URL + f"?n={len(calls)}"}})
            provider.send = send
            return provider

        with monkeypatch.context() as patcher:
            if repeated and disabled:
                from codey.operations.kernel_progress import KernelProgress

                patcher.setattr(KernelProgress, "stopped", property(lambda self: False))
            result = _run(tmp_path / str(disabled), budget=8 if repeated else (6 if disabled else 3),
                          required_checks=RESEARCH_ANSWER_REQUIRED_CHECKS,
                          provider_factory=factory)
        counts.append(len(calls))
        reasons.append(result.stop_reason)
    assert counts[0] < counts[1]
    if not repeated:
        assert counts == [3, 6]
    else:
        assert counts[1] == 8
    record("repeat_stop" if repeated else "request_budget", {"calls": counts[0], "stop": reasons[0]},
           {"calls": counts[1], "stop": reasons[1]}, boundary="original_research_kernel_scripted_provider",
           limitation="No real inference; disabled request budget uses a finite six-turn cap for safe observation.")


def test_redirect_fault(monkeypatch):
    original = source_download._checked_redirect(URL, "http://127.0.0.1/private", 0, content_kind="page")
    assert original.get("error")
    monkeypatch.setattr(source_download, "check_fetch_url", lambda *args, **kwargs: "")
    disabled = source_download._checked_redirect(URL, "http://127.0.0.1/private", 0, content_kind="page")
    assert disabled["url"] == "http://127.0.0.1/private" and not disabled.get("error")
    record("redirect_policy", "deny_private_redirect", "private_redirect_accepted", boundary="_checked_redirect",
           limitation="No private network request was made.")


def test_pdf_bound_and_close_fault(monkeypatch):
    class Response(io.BytesIO):
        headers = {"content-type": "application/pdf"}
        status = 200

        def geturl(self):
            return URL + "/paper.pdf"

    responses = []

    def opener(*args, **kwargs):
        response = Response(b"%PDF" + b"x" * 60)
        responses.append(response)
        return response

    monkeypatch.setattr(source_download, "check_fetch_url", lambda *args, **kwargs: "")
    monkeypatch.setattr(source_download, "_open_url_no_redirect", opener)
    monkeypatch.setattr(source_download, "PDF_MAX_BYTES", 32)
    original = source_download._download_pdf_streaming(URL + "/paper.pdf")
    assert original["status"] == "skipped" and responses[-1].closed
    monkeypatch.setattr(source_download, "PDF_MAX_BYTES", 1024)
    disabled = source_download._download_pdf_streaming(URL + "/paper.pdf")
    assert disabled["status"] == "ok" and len(disabled["bytes"]) == 64 and responses[-1].closed
    record("pdf_byte_bound", "skip_64_bytes_with_32_byte_cap_and_close", "accept_64_bytes_with_1024_byte_cap_and_close",
           boundary="_download_pdf_streaming", limitation="Tiny controlled cap; no real download or PDF parsing.")
    monkeypatch.setattr(Response, "close", lambda self: None)
    source_download._download_pdf_streaming(URL + "/paper.pdf")
    assert not responses[-1].closed
    io.BytesIO.close(responses[-1])
    record("download_resource_close", "response_closed", "response_left_open", boundary="response_context_exit",
           limitation="Controlled response adapter disables close, not a second downloader.")


def test_turn_snapshot_fault():
    from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.providers.base import AssistantTurn, ProviderToolCall
    from codey.toolchain.tool_spec import register_custom_tool, unregister_custom_tool

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), task_kind="chat")
    frozen = build_turn_snapshot(session, native=False)
    name = "review_late_registered_tool"
    assert register_custom_tool(name, grant="control")
    try:
        turn = AssistantTurn(tool_calls=(ProviderToolCall(id="c1", name=name, arguments={}),))
        assert normalize_turn(turn, snapshot=frozen).protocol_error
        rebuilt = build_turn_snapshot(session, native=False)
        assert not normalize_turn(turn, snapshot=rebuilt).protocol_error
    finally:
        unregister_custom_tool(name)
    record("turn_snapshot", "reject_tool_not_in_admitted_snapshot", "rebuilt_snapshot_accepts_late_tool",
           boundary="normalize_turn", limitation="Chat-scope tool registry boundary; no custom tool executed.")


def test_durable_receipt_loss_fault(tmp_path, monkeypatch):
    from codey.operations.task_effects import KernelEffectSink
    from codey.runtime.core.models import ToolCall, ToolResult
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.storage.managed_outputs import ManagedOutputStore
    from tests.support.receipt_fixtures import rebuild_settled_tool_result
    from tests.test_settled_delivery_recovery import _accept_and_mark, _new_log

    _, mutations, durable = _new_log(tmp_path, "session", "run")
    try:
        _accept_and_mark(mutations, "session", "run", str(tmp_path))
        managed = ManagedOutputStore(tmp_path / "state")
        sink = KernelEffectSink(mutations, session_id="session", run_id="run", provider_id="local",
                                managed_outputs=managed, durable=durable)
        call = ToolCall("open_url", {"url": URL}, call_id="read")
        body = "exact source\r\n中文材料\r\n" * 3000
        result = ToolResult(call, "bounded preview", ok=True, canonical={"complete_source": body})
        sink.begin_turn([("read-effect", call, 0)], turn=1)
        sink.settle("read-effect", True, result=result)
        projection = RuntimeEffectStore(durable=durable).load_effects("session", "run")[0]

        def restore():
            return rebuild_settled_tool_result(projection, managed_store=managed, session_id="session", run_id="run")

        original = restore()
        assert original is not None and original.canonical["complete_source"] == body
        monkeypatch.setattr(managed, "read_json_artifact", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))
        assert restore() is None
    finally:
        durable.close()
    record("durable_receipt", "full_source_restored", "missing_receipt_cannot_restore",
           boundary="real_kernel_settlement_and_restart_adapter",
           limitation="Loss simulated at managed read; original cold process recovery separately tested.")


@pytest.mark.usefixtures("public_source_dns")
def test_post_run_audit_removal_keeps_original_kernel_result(tmp_path):
    from tests.manual.research_process_review import ablation

    results = []
    for arm in ("baseline", "no_proof_audit"):
        with ablation(arm):
            result = run_whole(tmp_path / arm)
        calls = [json.loads(line) for line in (tmp_path / arm / "calls.jsonl").read_text().splitlines()]
        results.append((result.summary, result.accepted_report.state, result.turns, result.stop_reason,
                        sum("whole_request" in row for row in calls), sum("publication" in row for row in calls)))
    assert results[0] == results[1]
    assert results[0][2:] == (2, "done", 2, 1)
    record("post_run_proof_projection", {"turns": 2, "publications": 1}, {"turns": 2, "publications": 1},
           boundary="original_kernel_then_pipeline_projection",
           limitation="Identical scripted writer output; diagnostic trace loss is separate from answer quality.")


@pytest.mark.usefixtures("public_source_dns")
def test_state_field_removal_changes_open_question_completion(tmp_path):
    from codey.research.completion_gate import ResearchCompletionGate
    from tests.manual.research_process_review import ablation

    task = "Explain the writer limit."
    decisions = []
    for arm in ("baseline", "no_state_field"):
        calls = []

        def factory(provider, calls=calls, arm=arm):
            def send(prompt):
                calls.append(prompt)
                args = {"markdown": "Only one writer can write at a time [e1]."}
                if arm == "baseline":
                    args["state"] = "answered"
                return json.dumps({"tool": "open_url", "args": {"url": URL}} if len(calls) == 1 else
                                  {"tool": "submit_research_answer", "args": args})
            provider.send = send
            return provider

        with ablation(arm):
            result = _run(tmp_path / arm, native=True, task=task,
                          required_checks=RESEARCH_ANSWER_REQUIRED_CHECKS, provider_factory=factory)
        assert result.stop_reason == "done" and result.turns == 2
        decision = ResearchCompletionGate().evaluate(
            item=SimpleNamespace(kind="open_question", title=task, started_run_id="r"),
            event={"run_id": "r", "stop_reason": "done"}, research_result=result, session_id="s", task=task)
        decisions.append((result.accepted_report.state, result.accepted_report.open_questions, decision.block))
    assert decisions[0] == ("answered", (), False)
    assert decisions[1] == ("partial", (task,), True)
    record("declared_state_completion", {"state": "answered", "question_closed": True},
           {"state": "partial", "question_closed": False}, boundary="original_submission_publication_and_completion_gate",
           limitation="Same scripted correct body; model declarations still do not certify semantic correctness.")
