"""Exercise new host decisions using the same receipt and memory boundaries as production."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.app.knowledge_observation import project_completed_observations
from codey.app.memory_routes import memory_action
from codey.completion.contract import COMPLETION_COMPLETE, CompletionProof
from codey.durable.model import TaskCheckpoint
from codey.knowledge.archive import stable_identity
from codey.knowledge.model import KnowledgeMutation, KnowledgeObject
from codey.knowledge.store import KnowledgeStore
from codey.memory.candidates import candidate_from_payload
from codey.memory.context import retrieve_context
from codey.operations.kernel_contracts import DoneDecision, KernelResult
from codey.operations.project.edit_base import edit_base_version
from codey.operations.source_gaps import source_interaction_context
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.api_provider import ApiProvider
from codey.research.answer_contract import AnswerScope, digest, submit_answer
from codey.research.connector_search import ConnectorAwareSearchProvider
from codey.research.evidence_gaps import evidence_gaps, gap_search_candidates, same_source_groups
from codey.research.ledger import EvidenceItem, ResearchLedger
from codey.research.passages import ReadPassage
from codey.research.receipt_types import InvestigationRecord
from codey.research.reference import ResearchReference
from codey.research.report_evidence import check_report_evidence
from codey.research.source_document import OpenedSource, SourceDocument, SourcePage
from codey.research.source_gateway import ResearchSourceGateway
from codey.research.source_observation import snapshot_document, source_observation, source_snapshot
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.effects.effect_records import RuntimeEffectSettlement
from codey.toolchain.runtime import read_file, write_file
from codey.types.json_value import JsonObject, decode_json_object, require_json_string
from codey.types.tool_image import TOOL_IMAGES_KEY, ToolImageRef
from codey.workspace.context_admission import request_scope


@pytest.mark.parametrize("quote,extra,expected", [
    ("偏好 🥟", {}, (2, 6)), ("Rust", {}, None), ("Rust", {"quote_start": 16, "quote_end": 20}, (16, 20)),
    ("invented", {}, None), ("偏好 🥟", {"quote_start": 2}, None),
])
def test_host_locates_candidate_quote(quote: str, extra: JsonObject, expected: tuple[int, int] | None) -> None:
    row: JsonObject = {"kind": "preference", "text": "Use the examples I requested", "quote": quote, **extra}
    user_text = "我说偏好 🥟. Rust 和是 Rust"
    if expected is None:
        with pytest.raises(ValueError):
            candidate_from_payload(row, user_text=user_text, scope="user", at="2026-11-22T00:00:00+00:00")
        return
    draft = candidate_from_payload(row, user_text=user_text, scope="user", at="2026-11-22T00:00:00+00:00")
    assert (draft.quote_start, draft.quote_end) == expected
    assert user_text[draft.quote_start:draft.quote_end] == quote


def test_paged_edit_uses_raw_version_and_rejects_mixed_versions(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(b"first\r\nnext\nlast")
    session = TaskSession(TaskPolicy(frozenset({"project.read", "project.write"})))

    def read(identity: str, offset: int, limit: int) -> str:
        outcome = read_file(tmp_path, target.name, offset=offset, limit=limit)
        assert outcome.ok
        call = ToolCall("read_file", {"path": target.name, "offset": offset, "limit": limit})
        session._memory_results[identity] = ToolResult(call, outcome.model_text, ok=True,
            canonical=outcome.canonical, truncated=outcome.truncated)
        return identity

    first = read("first", 1, 1)
    target.write_bytes(b"first\nnext\nlast")
    read("new-tail", 2, 2)
    with pytest.raises(ValueError, match="missing line 2"):
        edit_base_version(session, tmp_path, target.name, first)
    target.write_bytes(b"first\r\nnext\nlast")
    read("original-tail", 2, 2)
    version = edit_base_version(session, tmp_path, target.name, first)
    target.write_bytes(b"first\nnext\nlast")
    assert not write_file(tmp_path, target.name, "replacement", expected_version=version).ok
    assert target.read_bytes() == b"first\nnext\nlast"


def test_confirmed_copy_survives_chat_delete_reopen_and_can_be_forgotten(tmp_path: Path) -> None:
    with closing(AppContext(state_home=tmp_path, memory_enabled=True, start_scheduler=False)) as ctx:
        ctx.durable.create_conversation("source")
        base: JsonObject = {"session_id": "source", "scope_kind": "session"}
        text = "Use concise examples in every project"
        status, body = memory_action(ctx, {**base, "action": "propose", "kind": "preference", "text": text,
                                            "slot": "response.detail", "value": "brief", "request_id": "propose"})
        assert status == 200
        proposal = ctx.memory.proposals(request_scope("", "source"))[0]
        command: dict[str, object] = {**base, "action": "confirm_user_preference", "proposal_id": require_json_string(body["proposal_id"], "proposal ID"),
                   "version": proposal.version, "text": text, "request_id": "confirm"}
        status, saved = memory_action(ctx, command)
        assert status == 200 and memory_action(ctx, command) == (status, saved)
        item_id = require_json_string(saved["item_id"], "item ID")
        ctx.durable.delete_conversation("source")
        ctx._delete_session_memory("source")
        assert ctx.memory.rebuild().events
    with closing(AppContext(state_home=tmp_path, memory_enabled=True, start_scheduler=False)) as ctx:
        ctx.durable.create_conversation("new")
        unrelated = tmp_path / "another-project"
        result = retrieve_context(ctx.memory, task="explain a loop", project=str(unrelated), session_id="new")
        assert text in result.text
        status, _ = memory_action(ctx, {"action": "revoke", "scope_kind": "user", "session_id": "new",
                                       "item_id": item_id, "request_id": "forget"})
        assert status == 200
        ctx.memory.rebuild()
        assert item_id not in {item.item_id for item in retrieve_context(ctx.memory, task=text, project=str(unrelated), session_id="new").items}


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_replay_remaps_ids_and_retains_every_tool_result_source(protocol: str) -> None:
    provider = ApiProvider("http://localhost:9/v1", "fixture", api_protocol=protocol)
    provider.bind_context("chat")
    call = {"id": "invalid/id", "function": {"name": "read", "arguments": "{}"}}
    image = ToolImageRef("state", "chat", "run", "captured", "f" * 64, 200, 10, 10, "observation")
    rows: list[dict[str, object]] = [
        {"role": "assistant", "tool_calls": [call], "reasoning_content": "private"},
        {"role": "tool", "tool_call_id": "invalid/id", "content": "first", TOOL_IMAGES_KEY: [image.payload()]},
        {"role": "tool", "tool_call_id": "invalid/id", "content": "second"},
    ]
    ids = ("call", "result-1", "result-2")
    provider.activate_durable_window(rows, source_entry_ids=ids, reason="model_change")
    view = provider._messages
    assert provider._durable_source_entry_ids == ids
    assert "private" not in str(view) and "invalid/id" not in str(view)
    assert "first" in str(view[1]) and "second" in str(view[2])
    assert "A prior tool result included an image" in str(view[1]) and "captured" not in str(view[1])
    previous = provider.window_ref
    with pytest.raises(ValueError, match="source IDs must match"):
        provider.activate_durable_window(rows, source_entry_ids=("one",), reason="model_change")
    assert provider.window_ref == previous and provider._messages == view


def test_quote_is_bound_to_the_selected_passage_and_uses_original_offsets() -> None:
    text = "Header. The Cedar team measured  24 kg."
    source = OpenedSource("https://example.org/a", "https://example.org/a", "Cedar", "", "version")
    ledger = ResearchLedger()
    ledger.read_passages["passage"] = ReadPassage("passage", source, text, (), 8, len(text))
    scope = AnswerScope(digest("task"), (("e1", "passage"),), ("attempt",))
    valid = submit_answer(scope, {"markdown": '"The Cedar team measured 24 kg" [e1]', "state": "answered"})
    evidence = check_report_evidence(valid, ledger.read_passages)
    rows = evidence["verified_quotes"]
    assert isinstance(rows, list) and len(rows) == 1
    assert decode_json_object(rows[0])["start"] == 8
    wrong = submit_answer(scope, {"markdown": '"The Birch team measured 24 kg" [e1]', "state": "answered"})
    with pytest.raises(ValueError, match="absent from its bound passage"):
        check_report_evidence(wrong, ledger.read_passages)


def test_gap_query_is_fenced_by_budget_and_attempted_receipts() -> None:
    ledger = ResearchLedger()
    ledger.questions = ("Atlas 10 L controlled fuel consumption measurement",)
    claim = "Atlas unit fuel consumption under the 10 L workload and at 20 °C"
    ledger.references["f1"] = ResearchReference("cedar-finding", 1, "claim", "current_run", "cedar", question_ref="q1")
    ledger.references["f2"] = ResearchReference("birch-finding", 1, "claim", "current_run", "birch", question_ref="q1")
    ledger.evidence_items.extend((EvidenceItem(claim, "https://example.com", "24 kg", note_id="cedar"),
                                  EvidenceItem(claim, "https://example.net", "31 kg", "contradicts", "birch")))
    assert evidence_gaps(ledger, ledger.questions)[0].reason == "conflicting_findings"
    assert gap_search_candidates(ledger, max_queries=0) == []
    suggested = gap_search_candidates(ledger, max_queries=1)
    assert len(suggested) == 1
    query = require_json_string(suggested[0]["query"], "query")
    attempt: InvestigationRecord = {"tool": "web_search", "ok": False, "question_ref": "q1", "args": {"query": query}, "passage_refs": []}
    ledger.investigations["a1"] = attempt
    assert gap_search_candidates(ledger, max_queries=1) == []


def test_exact_reposts_cluster_without_merging_versions_or_independent_passages() -> None:
    ledger = ResearchLedger()
    text = "An exact repost of the complete original research evidence. " * 4
    for index, url in enumerate(("https://example.com/original", "https://example.net/copy", "https://example.org/replication"), 1):
        version = "original-content" if index < 3 else "different-content"
        source = OpenedSource(url, url, "Source", "", version)
        ref = f"p{index}"
        ledger.read_passages[ref] = ReadPassage(ref, source, text if index < 3 else "An independent replication", (), 0, len(text))
    aliases = (("e1", "p1"), ("e2", "p2"), ("e3", "p3"))
    assert same_source_groups(ledger.read_passages, aliases) == [["e1", "e2"]]
    assert set(ledger.read_passages) == {"p1", "p2", "p3"}


def test_browser_observation_is_versioned_scoped_and_rechecked_when_its_source_changes(tmp_path: Path) -> None:
    from dataclasses import replace

    project = str(tmp_path.resolve())
    url, version = "https://example.org/record", "original-capture"
    with closing(KnowledgeStore(tmp_path / "knowledge")) as store:
        source_id = stable_identity("source", "project", project, url, version)
        source_provenance: JsonObject = {"url": url, "content_hash": version}
        source = KnowledgeObject(source_id, "source", "Original contents", "source", scope="project", scope_id=project,
                                 provenance=source_provenance, status="active")
        store.commit_knowledge("original-source", [KnowledgeMutation(source)])
        task = TaskCheckpoint("run", "chat", "submitted", "completed", 2, {}, {"project": project, "stop_reason": "done"})
        decision = DoneDecision(purpose="done", result=KernelResult(True, "done", 4, "done").to_payload(),
                                workspace_revision=0, workspace_fingerprint="")
        receipt = ToolResult(ToolCall("browser_read", {}), "private body", ok=True, audit={"query_click": "click-1"},
            canonical={"research_observation": {"source": {"text_hash": version, "final_url": url}, "text": "private body"}})
        settlement = RuntimeEffectSettlement("bound-read", "tool_call", "chat", "run")
        facts = project_completed_observations(task, [(receipt, settlement)], decision=decision, decision_ref="done-ref", store=store)
        assert len(facts) == 1
        assert not project_completed_observations(replace(task, phase="failed", status=None), [(receipt, settlement)],
                                                  decision=decision, decision_ref="done-ref", store=store)
        first = store.commit_knowledge("terminal-observation:run", facts)
        assert store.commit_knowledge("terminal-observation:run", facts) == first
        fresh = store.search("same-document result update", project=project, session_id="new", depth=0)
        assert len(fresh) == 1 and fresh[0].version.revision == 1
        assert "private body" not in str(fresh[0].to_dict())
        assert store.search("same-document result update", project=project + "-other", session_id="new", depth=0) == []
        store.commit_knowledge("revised-source", [KnowledgeMutation(replace(source, content="Changed contents"), expected_revision=1)])
        assert store.search("same-document result update", project=project, session_id="new", depth=0)[0].version.dependencies_changed


def test_project_observation_records_a_repaired_check_with_both_workspace_versions(tmp_path: Path) -> None:
    task = TaskCheckpoint("run", "chat", "submitted", "completed", 2, {},
        {"project": str(tmp_path), "stop_reason": "done"})
    proof = CompletionProof("proof", "project", "project", "run", COMPLETION_COMPLETE, True, "", (), (), (), (), (), (), (), ())
    before, after = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    decision = DoneDecision(purpose="done", result=KernelResult(True, "done", 4, "done", proof).to_payload(),
        workspace_revision=2, workspace_fingerprint=after)
    call = ToolCall("run", {"command": "python -m pytest", "path": "."})
    failed = ToolResult(call, "check failed", ok=False,
        audit={"workspace_revision": 1, "workspace_fingerprint": before})
    updated = {"workspace_revision": 2, "workspace_fingerprint": after}
    edit = ToolResult(ToolCall("edit", {"path": "importer.py"}), "edited", ok=True, audit={**updated, "changed": True})
    passed = ToolResult(call, "check passed", ok=True, audit=updated)
    facts = project_completed_observations(task, [
        (failed, RuntimeEffectSettlement("failed", "tool_call", "chat", "run", status="error", exit_code=1)),
        (edit, RuntimeEffectSettlement("edited", "tool_call", "chat", "run")),
        (passed, RuntimeEffectSettlement("passed", "tool_call", "chat", "run", exit_code=0)),
    ], decision=decision, decision_ref="done")
    observation, = facts
    repairs = observation.value.provenance["repaired_checks"]
    assert isinstance(repairs, list)
    row = decode_json_object(repairs[0])
    assert decode_json_object(row["failed"])["workspace_fingerprint"] == before
    assert decode_json_object(row["passed"])["effect_id"] == "passed"
    assert observation.value.provenance["workspace_fingerprint"] == after


def test_source_interaction_hint_clears_after_an_original_source_read() -> None:
    url = "https://example.org/record"
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read"})))
    session._memory_results["failed"] = ToolResult(ToolCall("open_url", {"url": url}), "login", ok=False,
        canonical={"source_failure": {"url": url, "detail": "sign in", "code": "login_required"}})
    assert '"state": "needs_human"' in source_interaction_context(session)
    session.record_open(url)
    assert source_interaction_context(session) == ""


@pytest.mark.parametrize("pdf_available", [True, False])
def test_arxiv_full_text_reuses_admitted_provider_and_keeps_original_attempt(
    monkeypatch: pytest.MonkeyPatch, pdf_available: bool,
) -> None:
    from tests.test_research import fake_pypdf

    abstract = "https://arxiv.org/abs/2401.01234v1"
    full_text = "https://arxiv.org/pdf/2401.01234v1"
    requested: list[str] = []
    admitted: list[str] = []

    def admit(url: str, *, use_cache: bool) -> None:
        admitted.append(url)

    class Sources:
        def search(self, query: str, *, limit: int) -> list[JsonObject]:
            return []

        def fetch(self, url: str) -> dict[str, object]:
            requested.append(url)
            if url == abstract:
                return {"status": "ok", "url": url, "title": "Fixture", "text": "This text is an abstract.", "content_kind": "abstract"}
            if pdf_available:
                return {"status": "ok", "url": url, "title": "Fixture", "content_kind": "pdf", "bytes": b"%PDF-file"}
            raise TimeoutError("full text timed out")

    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", admit)
    ledger = ResearchLedger()
    with fake_pypdf("A complete source page."):
        result = ResearchSourceGateway(Sources(), ledger).open(abstract)
    assert result.status == "ok"
    assert requested == [abstract, full_text] and {abstract, full_text} <= set(admitted)
    assert result.document is not None
    assert result.document.content_kind == ("pdf" if pdf_available else "abstract")
    assert bool(result.document.pdf_sha256) is pdf_available
    attempts = result.acquisition.get("attempts")
    assert isinstance(attempts, list) and len(attempts) == 2
    assert decode_json_object(attempts[-1])["status"] == ("ok" if pdf_available else "error")


@pytest.mark.parametrize("full_text_available", [True, False])
def test_pubmed_retains_confirmed_retraction_when_opening_or_falling_back_from_pmc(
    monkeypatch: pytest.MonkeyPatch, full_text_available: bool,
) -> None:
    pubmed = "https://pubmed.ncbi.nlm.nih.gov/12345678/"
    pmc = "https://pmc.ncbi.nlm.nih.gov/articles/PMC12345/"
    doi = "https://doi.org/10.5555/fixture"
    requests: list[str] = []
    failures: list[str] = []

    class Sources:
        def search(self, query: str, *, limit: int) -> list[JsonObject]:
            return []

        def fetch(self, url: str) -> dict[str, object]:
            requests.append(url)
            if full_text_available:
                return {"status": "ok", "url": url, "text": "This is the complete archived document."}
            raise TimeoutError("repository did not respond")

    class Connector(ConnectorAwareSearchProvider):
        def _read_connector_url(self, connector_id: str, url: str) -> str:
            assert connector_id == "pubmed" and "12345678" in url
            return """<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>12345678</PMID>
            <Article><ArticleTitle>Archived study</ArticleTitle><Abstract><AbstractText>Original abstract.</AbstractText></Abstract>
            <PublicationTypeList><PublicationType>Retracted Publication</PublicationType></PublicationTypeList>
            </Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="doi">10.5555/Fixture</ArticleId>
            <ArticleId IdType="pmc">PMC12345</ArticleId></ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>"""

    def admit(url: str, *, use_cache: bool) -> None:
        assert url in {pmc, pubmed, doi}

    def record_failure(area: str, action: str, error: object, *, url: str = "") -> None:
        failures.append(str(error))

    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", admit)
    ledger = ResearchLedger()
    result = ResearchSourceGateway(Connector(Sources()), ledger, on_failure=record_failure).open(pubmed)
    assert result.status == "ok"
    assert requests == ([pmc] if full_text_available else [pmc, doi])
    attempts = result.acquisition["attempts"]
    assert isinstance(attempts, list)
    assert [decode_json_object(row)["requested_url"] for row in attempts] == [pubmed, *requests]
    assert result.document == snapshot_document(result.observation)
    assert result.document is not None and result.document.publication is not None
    assert result.document.publication.status == "retracted"
    assert result.document.publication.doi == "10.5555/fixture"
    assert result.document.final_url == (pmc if full_text_available else pubmed)
    assert result.document.content_kind == ("html" if full_text_available else "abstract")
    assert ("repository did not respond" in failures) is not full_text_available


def test_pdf_pages_from_different_downloads_do_not_share_a_source_version() -> None:
    alias = "https://example.com/latest.pdf"
    ledger = ResearchLedger()
    captures: list[JsonObject] = []
    for revision, page, text in (("a", 1, "Old result"), ("b", 2, "New second page"), ("b", 3, "New third page")):
        document = SourceDocument(alias, alias, content_kind="pdf", text=text, pages_read=(page,), page_count=3,
                                  page_texts=(SourcePage(page, text),), pdf_sha256=revision * 64)
        source = ledger.record_open_document(document)
        assert source is not None
        captures.append(source_snapshot(source, text, {page: text}))

    recovered = ResearchLedger()
    for observation in reversed(captures):
        recovered.merge_opened_source(*source_observation(observation))

    assert ledger.source_pages_for_url(alias) == recovered.source_pages_for_url(alias) == {
        2: "New second page", 3: "New third page",
    }
    assert ledger.opened_sources == recovered.opened_sources
