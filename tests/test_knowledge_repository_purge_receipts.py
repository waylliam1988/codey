from contextlib import closing
from pathlib import Path

import pytest

from codey.knowledge.model import KnowledgeMutation, KnowledgeObject
from codey.knowledge.store import KnowledgeStore


def test_purge_revokes_receipts_for_reused_staged_versions(tmp_path: Path) -> None:
    with closing(KnowledgeStore(tmp_path)) as store:
        repository = store.repository
        entity = KnowledgeObject("shared-entity", "entity", "Private subject", "user_statement",
                                 scope="session", scope_id="owner")
        mutation = KnowledgeMutation(entity)
        assert repository.commit("original", [mutation]) == {entity.id: 1}
        assert repository.commit("staged", [mutation], shared_staged=True) == {entity.id: 1}
        assert repository.committed("staged", [mutation]) == {entity.id: 1}

        unrelated = KnowledgeMutation(KnowledgeObject("other", "claim", "Other material", "model_inference"))
        assert repository.commit("other", [unrelated]) == {"other": 1}
        assert store.purge_knowledge([entity.id]) == {entity.id}
        assert repository.read(entity.id) is None
        assert repository.committed("other", [unrelated]) == {"other": 1}

    with closing(KnowledgeStore(tmp_path)) as reopened:
        for commit_id in ("original", "staged"):
            with pytest.raises(ValueError, match="purged content"):
                reopened.repository.committed(commit_id, [mutation])
            with pytest.raises(ValueError, match="purged content"):
                reopened.repository.commit(commit_id, [mutation], shared_staged=True)
