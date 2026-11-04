"""Safety checks for the independently measured simplifications."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codey.knowledge.model import KnowledgeMutation, KnowledgeObject
from codey.knowledge.store import KnowledgeStore
from codey.research.answer_contract import answer_scope
from codey.runtime.core.models import ToolCall, ToolResult
from tests.manual.research_revision_longread_ab import committed_view, expanded_read, revision_args, treatment
from tests.test_research_findings import URL, setup


def test_revision_derives_read_version_and_preserves_unmentioned_fields(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        value = KnowledgeObject("claim", "claim", "Original content", "model_extraction", title="Before",
                                aliases=("old",), conditions={"mode": {"state": "reported", "value": "WAL"}},
                                scope="personal", scope_id=store.owner_id, provenance={"original": True})
        store.repository.commit("seed", [KnowledgeMutation(value)])
        session = SimpleNamespace(_memory_results={"read": ToolResult(
            ToolCall("knowledge_read", {"id": "claim", "revision": 1}),
            json.dumps(store.repository.read("claim", 1).to_dict()), ok=True)})
        args = {"commit_id": "edit", "changes": [{"value": {"id": "claim", "title": "After"}}]}
        result = revision_args(session, store, args)
        assert result["changes"] == [{"expected_revision": 1, "value": replace(value, title="After").to_dict()}]
        store.repository.commit("concurrent", [KnowledgeMutation(replace(value, title="Concurrent"), 1)])
        assert revision_args(session, store, args) == result  # Never refresh silently to current HEAD.
        with pytest.raises(ValueError):
            store.repository.commit("edit", [KnowledgeMutation(KnowledgeObject.from_dict(result["changes"][0]["value"]), 1)])
        with pytest.raises(ValueError, match="Read this exact"):
            revision_args(SimpleNamespace(_memory_results={}), store, args)
        with pytest.raises(ValueError, match="program supplies"):
            revision_args(session, store, {**args, "changes": [{"value": {"id": "claim"}, "expected_revision": 2}]})
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_full_read_fits_all_originals_and_preserves_explicit_limit_and_budget(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    session.task_kind = "coding"  # Isolated maintenance experiment, outside Research.
    try:
        delegate._research_scope = answer_scope(session)
        tools.coherent_passages = True
        tools.search.text = "## Introduction\n\n" + "Complete archive sentence. " * 350 + "\n\n## Important tail\n\nOR not AND."
        opened = tools.open_url(URL)
        assert opened.read_passages[-1]["end"] < len(tools.search.text)
        result = expanded_read(delegate, ToolCall("open_url", {"url": URL}), opened, None)
        assert result.read_passages[-1]["end"] == len(tools.search.text)
        assert "OR not AND." in result.model_text
        assert not session._memory_results  # Projection is pure until the real receipt settles.
        assert expanded_read(delegate, ToolCall("open_url", {"url": URL, "limit": 500}), opened, None) is opened
        provider = SimpleNamespace(context_budget=SimpleNamespace(window_tokens=100, input_limit=100))
        assert expanded_read(delegate, ToolCall("open_url", {"url": URL}), opened, provider) is opened
    finally:
        store.close()


@pytest.mark.parametrize("ok,wrong_id", [(False, False), (True, True)])
def test_revision_does_not_use_failed_or_wrong_object_reads(tmp_path, ok, wrong_id):
    store = KnowledgeStore(tmp_path)
    try:
        value = KnowledgeObject("claim", "claim", "Original", "model_extraction")
        store.repository.commit("seed", [KnowledgeMutation(value)])
        session = SimpleNamespace(_memory_results={"read": ToolResult(
            ToolCall("knowledge_read", {"id": "other" if wrong_id else "claim"}),
            json.dumps(store.repository.read("claim").to_dict()), ok=ok)})
        with pytest.raises(ValueError, match="Read this exact"):
            revision_args(session, store, {"commit_id": "edit", "changes": [{"value": {"id": "claim", "title": "After"}}]})
    finally:
        store.close()


def test_revision_keeps_new_creation_and_repository_idempotency(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        value = KnowledgeObject("new", "entity", "Explicit new entity", "user_statement",
                                scope="personal", scope_id=store.owner_id)
        session = SimpleNamespace(_memory_results={})
        args = revision_args(session, store, {"commit_id": "create", "changes": [{"value": value.to_dict()}]})
        assert args["changes"] == [{"value": value.to_dict(), "expected_revision": 0}]
        mutations = [KnowledgeMutation(value)]
        receipt = store.repository.commit("create", mutations)
        assert store.repository.commit("create", mutations) == receipt
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_full_read_budget_includes_existing_material_and_uses_provider_count(tmp_path):
    from tests.test_research_findings import execute

    store, tools, session, delegate = setup(tmp_path)
    session.task_kind = "coding"  # Isolated maintenance experiment, outside Research.
    try:
        delegate._research_scope = answer_scope(session)
        session._memory_results["old"] = execute(delegate, tools, session, "open_url", {"url": URL})
        tools.coherent_passages = True
        tools.search.text = "## Long source\n\n" + "New original sentence. " * 420 + "\n\nTail condition."
        opened = tools.open_url(URL + "/next")
        seen = []

        def count(text, **kwargs):
            seen.append(text)
            return SimpleNamespace(value=5001)

        provider = SimpleNamespace(context_budget=SimpleNamespace(window_tokens=10000, input_limit=9000), count_text=count)
        delegate._research_scope = answer_scope(session)
        assert expanded_read(delegate, ToolCall("open_url", {"url": URL + "/next"}), opened, provider) is opened
        assert len(seen) == 1 and "Writers and readers" in seen[0] and "Tail condition." in seen[0]
        assert list(session._memory_results) == ["old"]
    finally:
        store.close()


@pytest.mark.parametrize("patch_value", [
    {"kind": "source"}, {"scope": "personal", "scope_id": "another-owner"}, {"unknown_field": "invalid"},
])
def test_revision_retains_kind_access_and_exact_field_guards(tmp_path, patch_value):
    store, tools, session, delegate = setup(tmp_path)
    session.task_kind = "coding"  # Isolated maintenance experiment, outside Research.
    try:
        value = KnowledgeObject("claim", "claim", "Original", "model_extraction", scope="personal", scope_id=store.owner_id)
        store.repository.commit("seed", [KnowledgeMutation(value)])
        delegate._research_scope = answer_scope(session)
        read, _, _ = delegate._execute_research(ToolCall("knowledge_read", {"id": "claim", "revision": 1}))
        assert read.ok
        session._memory_results["read"] = read
        with treatment("revision"):
            result, _, _ = delegate._execute_research(ToolCall("knowledge_commit", {
                "commit_id": "bad", "changes": [{"value": {"id": "claim", **patch_value}}]}))
        assert not result.ok
        assert store.repository.read("claim").revision == 1
    finally:
        store.close()


def test_revision_identical_retry_and_invalid_second_change_are_atomic(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    session.task_kind = "coding"  # Isolated maintenance experiment, outside Research.
    try:
        value = KnowledgeObject("claim", "claim", "Original", "model_extraction")
        store.repository.commit("seed", [KnowledgeMutation(value)])
        delegate._research_scope = answer_scope(session)
        read, _, _ = delegate._execute_research(ToolCall("knowledge_read", {"id": "claim"}))
        assert read.ok
        session._memory_results["read"] = read
        args = {"commit_id": "edit", "changes": [{"value": {"id": "claim", "title": "After"}}]}
        with treatment("revision"):
            bad, _, _ = delegate._execute_research(ToolCall("knowledge_commit", {
                **args, "changes": [*args["changes"], {"value": {"id": "new-incomplete"}}]}))
            assert not bad.ok and store.repository.read("claim").revision == 1
            first, _, _ = delegate._execute_research(ToolCall("knowledge_commit", args))
            again, _, _ = delegate._execute_research(ToolCall("knowledge_commit", args))
        assert first.ok and again.ok and first.model_text == again.model_text
        assert first.call.args == args and store.repository.read("claim").revision == 2
    finally:
        store.close()


def test_flat_revision_format_and_success_receipt_are_directly_usable(tmp_path):
    from codey.research.answer_context import _render_context

    store, tools, session, delegate = setup(tmp_path)
    session.task_kind = "coding"  # Isolated maintenance experiment, outside Research.
    try:
        value = KnowledgeObject("claim", "claim", "Original", "model_extraction")
        store.repository.commit("seed", [KnowledgeMutation(value)])
        delegate._research_scope = answer_scope(session)
        read, _, _ = delegate._execute_research(ToolCall("knowledge_read", {"id": "claim"}))
        session._memory_results["read"] = read
        call = ToolCall("knowledge_commit", {"commit_id": "edit", "changes": [{"id": "claim", "title": "After"}]})
        with treatment("revision_flat"):
            result, _, _ = delegate._execute_research(call)
        assert result.ok, result.model_text
        session._memory_results["commit"] = result
        scope = answer_scope(session)
        text = committed_view(_render_context, session, tools.ledger, scope, scope.aliases)
        assert result.model_text in text and "already committed" in text
        complete = committed_view(_render_context, session, tools.ledger, scope, scope.aliases, include_changes=True)
        assert '"changes":[{"id":"claim","title":"After"}]' in complete
        assert "not evidence" in complete
        assert store.repository.read("claim").value == replace(value, title="After")
    finally:
        store.close()


def test_unchanged_edits_cannot_create_another_version_after_reread(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    session.task_kind = "coding"  # Isolated maintenance experiment, outside Research.
    try:
        store.repository.commit("seed", [KnowledgeMutation(KnowledgeObject("claim", "claim", "Original", "model_extraction"))])
        delegate._research_scope = answer_scope(session)
        read, _, _ = delegate._execute_research(ToolCall("knowledge_read", {"id": "claim", "revision": 1}))
        session._memory_results["read"] = read
        with treatment("revision_flat_noop"):
            args = {"commit_id": "edit", "changes": [{"id": "claim", "title": "After"}]}
            first, _, _ = delegate._execute_research(ToolCall("knowledge_commit", args))
            assert first.ok
            session._memory_results["edit"] = first
            read, _, _ = delegate._execute_research(ToolCall("knowledge_read", {"id": "claim", "revision": 2}))
            session._memory_results["read2"] = read
            again, _, _ = delegate._execute_research(ToolCall("knowledge_commit", {**args, "commit_id": "edit-again"}))
            assert not again.ok and "No fields differ" in again.model_text
            assert store.repository.read("claim").revision == 2
    finally:
        store.close()








def test_explicit_creation_candidate_never_infers_creation_from_unknown_edit_id(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    session.task_kind = "coding"  # Isolated maintenance experiment, outside Research.
    try:
        delegate._research_scope = answer_scope(session)
        value = KnowledgeObject("new-entity", "entity", "Explicitly requested entity", "user_statement",
                                scope="personal", scope_id=store.owner_id)
        with treatment("revision_explicit_create"):
            unknown, _, _ = delegate._execute_research(ToolCall("knowledge_commit", {
                "commit_id": "typo", "changes": [value.to_dict()]}))
            assert not unknown.ok and store.repository.read(value.id) is None
            args = {"commit_id": "create", "changes": [{**value.to_dict(), "create": True}]}
            created, _, _ = delegate._execute_research(ToolCall("knowledge_commit", args))
            again, _, _ = delegate._execute_research(ToolCall("knowledge_commit", args))
            assert created.ok and again.ok and created.model_text == again.model_text
            overwrite, _, _ = delegate._execute_research(ToolCall("knowledge_commit", {
                "commit_id": "overwrite", "changes": [{**value.to_dict(), "content": "Cannot overwrite", "create": True}]}))
            assert not overwrite.ok
            assert store.repository.read(value.id).value == value
            assert store.repository.read(value.id).revision == 1
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("token_count", [4000, 5001, None])
def test_production_full_read_keeps_old_originals_and_restores_real_receipts(tmp_path, token_count):
    from codey.operations.kernel_facts import project_result_observation
    from codey.providers.token_accounting import ContextBudget
    from codey.research.answer_context import fitting_full_read
    from codey.research.ledger import ResearchLedger
    from tests.test_research_findings import execute

    store, tools, session, delegate = setup(tmp_path)
    session.task_kind = "coding"  # Isolated maintenance experiment, outside Research.
    try:
        delegate._research_scope = answer_scope(session)
        session._memory_results["old"] = execute(delegate, tools, session, "open_url", {"url": URL})
        tools.coherent_passages = True
        tools.search.text = "## Long source\n\n" + "New original sentence. " * 420 + "\n\nTail condition."
        seen = []

        def count(text, **kwargs):
            seen.append(text)
            if token_count is None:
                raise TimeoutError("tokenizer unavailable")
            return SimpleNamespace(value=token_count)

        provider = SimpleNamespace(context_budget=ContextBudget(10000, 1000, 0, 1000), count_text=count)
        tools.context_provider = provider
        opened = delegate._observation_tools(source_read=True).open_url(URL + "/next")
        for args in ({"limit": 500}, {"pages": "1"}):
            assert fitting_full_read(session, tools.ledger,
                                     ToolCall("open_url", {"url": URL + "/next", **args}), opened,
                                     provider=provider) is opened
        assert not seen
        read = execute(delegate, tools, session, "open_url", {"url": URL + "/next"})
        assert len(seen) == 1 and "Writers and readers" in seen[0] and "Tail condition." in seen[0]
        end = read.canonical["read_passages"][-1]["end"]
        assert (end == len(tools.search.text)) == (token_count == 4000)
        assert list(session._memory_results) == ["old"]
        cold = ResearchLedger()
        for result in (session._memory_results["old"], read):
            project_result_observation(session, result.call, result, ok=True, research_ledger=cold)
        assert cold.read_passages == tools.ledger.read_passages
    finally:
        store.close()
