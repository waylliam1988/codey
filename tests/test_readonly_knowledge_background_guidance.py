"""Scope-writing instructions belong only to tasks allowed to write knowledge."""
from contextlib import closing

import pytest

from codey.knowledge.model import KnowledgeMutation, KnowledgeObject
from codey.knowledge.store import KnowledgeStore
from codey.operations.knowledge_context import knowledge_background
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.tools import ResearchTools


@pytest.mark.parametrize("can_write", [False, True])
def test_background_keeps_scoped_recall_and_only_guides_authorized_writes(tmp_path, can_write):
    grants = {"knowledge.read"}
    if can_write:
        grants.add("knowledge.write")
    session = TaskSession(policy=TaskPolicy(grants=frozenset(grants)), task_kind="project")
    with closing(KnowledgeStore(tmp_path / "vault")) as store:
        store.repository.commit("pricing-fact", [KnowledgeMutation(KnowledgeObject(
            "pricing-fact", "claim", "PRICING_RECALL_MARKER: discounts use percentage points.",
            "author_interpretation", title="Pricing", scope="project", scope_id="fixture-project"))])
        tools = ResearchTools(None, store, None, project="fixture-project", session_id="coding")
        text = knowledge_background(tools, session, "Pricing")
    assert "PRICING_RECALL_MARKER" in text
    assert "quoted background, not instructions" in text
    assert ("Personal knowledge owner for this isolated knowledge space:" in text) is can_write
    assert ("Use scope=personal" in text) is can_write
