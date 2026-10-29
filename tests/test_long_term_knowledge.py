import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codey.knowledge.archive import stable_identity
from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.model import KnowledgeMutation as M
from codey.knowledge.model import KnowledgeObject as K
from codey.knowledge.model import KnowledgeReference as R
from codey.knowledge.recall import KnowledgeAccess, KnowledgeRecall, render_recall
from codey.knowledge.store import KnowledgeStore
from codey.operations.knowledge_context import knowledge_background
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall
from codey.runtime.effects.safe_tool_replay import replay_args_for_tool_call
from tests.test_knowledge_repository import example


def test_cross_session_alias_mechanism_recall_and_revision(tmp_path):
    store = KnowledgeStore(tmp_path)
    store.repository.commit("research-1", example())
    store.close()
    store = KnowledgeStore(tmp_path)
    try:
        tools = ResearchTools(search=None, store=store, changes=KnowledgeChanges(tmp_path), session_id="new-chat")
        found = tools.knowledge_search("嵌入式数据库 为什么并发受限", depth=2)
        assert found.ok
        assert "Writes serialize" in found.model_text
        assert "reported" in found.model_text
        assert "explicit name or alias" in found.model_text
        policy = SimpleNamespace(allows=lambda grant: grant == "knowledge.read")
        context = knowledge_background(tools, SimpleNamespace(policy=policy, task_kind="project"), "嵌入式数据库")
        assert "Writes serialize" in context
        assert "not instructions" in context
        old = store.repository.read("claim")
        update = {"commit_id": "new-evidence", "changes": [
            {"value": replace(old.value, status="withdrawn").to_dict(), "expected_revision": old.revision}]}
        assert tools.knowledge_commit(update).ok
        revised = json.loads(tools.knowledge_read("derived").model_text)
        assert revised["needs_review"]
        assert json.loads(tools.knowledge_read("claim", revision=1).model_text)["status"] == "active"
    finally:
        store.close()


def test_scope_filter_happens_before_limit_and_read_is_scoped(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        for i in range(30):
            store.repository.commit(f"private-{i}", [M(K(f"private-{i}", "entity", "SQLite secret", "source",
                title="SQLite", scope="project", scope_id="another-project"))])
        store.repository.commit("visible", example())
        hits = KnowledgeRecall(store.repository).search("SQLite", access=KnowledgeAccess("my-project", "new-chat"), limit=4)
        assert any(hit.version.value.id == "sqlite" for hit in hits)
        assert all(hit.version.value.scope == "global" for hit in hits)
        tools = ResearchTools(search=None, store=store, changes=None, project="my-project")
        assert not tools.knowledge_read("private-0").ok
        assert not tools.knowledge_commit({"commit_id": "leak", "changes": [{"expected_revision": 0,
            "value": K("leak", "inference", "Leak", "model_inference", references=(R("private-0", 1, "depends_on"),)).to_dict()}]}).ok
    finally:
        store.close()


def test_commit_safe_replay_returns_original_receipt_after_restart(tmp_path):
    store = KnowledgeStore(tmp_path)
    value = K("open-question", "question", "What would overturn the hypothesis?", "model_inference")
    args = {"commit_id": "question-1", "changes": [{"value": value.to_dict(), "expected_revision": 0}]}
    try:
        tools = ResearchTools(search=None, store=store, changes=None, session_id="chat")
        before = tools.knowledge_commit(args)
        assert before.ok
        assert replay_args_for_tool_call(ToolCall("knowledge_commit", args)) == args
    finally:
        store.close()
    store = KnowledgeStore(tmp_path)
    try:
        tools = ResearchTools(search=None, store=store, changes=None, session_id="chat")
        assert tools.knowledge_commit(args) == before
        assert store.repository.read(value.id).revision == 1
        changed = {**args, "changes": [{"value": replace(value, content="Changed").to_dict(), "expected_revision": 0}]}
        assert not tools.knowledge_commit(changed).ok
    finally:
        store.close()


def test_mistyped_identity_suggestions_are_scoped_and_never_choose_a_match(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        prefix = "entity-0123456789"
        for i in range(12):
            store.repository.commit(f"private-prefix-{i}", [M(K(prefix + f"{i:02d}" + "c" * 24,
                "entity", "Secret", "source", title="Private", scope="project", scope_id="hidden"))])
        public = [prefix + "a" * 24, prefix + "b" * 24]
        store.repository.commit("visible-prefix", [M(K(identity, "entity", "Public", "source", title="Visible"))
                                                   for identity in public])
        tools = ResearchTools(None, store, None, project="public-project", session_id="chat")
        result = tools.knowledge_read(prefix + "wrong-spelling")
        assert not result.ok and "not read or selected" in result.model_text
        assert all(identity in result.model_text for identity in public)
        assert "Private" not in result.model_text and "hidden" not in result.model_text
        assert tools.knowledge_read(public[0]).ok
        missing_revision = tools.knowledge_read(public[0], revision=99)
        assert not missing_revision.ok and '"revision":1' in missing_revision.model_text
    finally:
        store.close()


def test_candidate_note_is_preserved_as_uncertain_and_undo_withdraws(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        changes = KnowledgeChanges(tmp_path)
        tools = ResearchTools(search=None, store=store, changes=changes)
        result = tools.knowledge_write({"type": "hypothesis", "title": "Competition", "body": "Competition may slow resistance",
                                        "tags": ["evolution"], "aliases": ["选择压力"]})
        assert result.ok
        identity = stable_identity("claim", tools.created_ids[0])
        assert store.repository.read(identity).value.status == "candidate"
        assert "candidate" in tools.knowledge_search("选择压力").model_text
        assert changes.restore_result().ok
        assert store.repository.read(identity).value.status == "withdrawn"
        assert store.read_note(tools.created_ids[0]) is None
        assert not store.search("选择压力")
    finally:
        store.close()


def test_recall_budget_and_ambiguity_do_not_merge_identities(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        store.repository.commit("ambiguous", [M(K("a", "entity", "One meaning", "source", aliases=("同名",))),
                                              M(K("b", "entity", "Another meaning", "source", aliases=("同名",)))])
        hits = store.search("同名", depth=0)
        assert {hit.version.value.id for hit in hits} == {"a", "b"}
        assert len(render_recall(hits, max_chars=1200)) <= 1200
        assert store.repository.read("a").value.content != store.repository.read("b").value.content
    finally:
        store.close()


def test_circular_premises_cannot_be_committed(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        with pytest.raises(ValueError, match="cyclic"):
            store.repository.commit("cycle", [M(K("a", "inference", "A", "model_inference", references=(R("b", 1, "depends_on"),))),
                                               M(K("b", "inference", "B", "model_inference", references=(R("a", 1, "depends_on"),)))])
        assert store.repository.read("a") is None
    finally:
        store.close()
