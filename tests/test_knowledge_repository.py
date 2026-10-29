from dataclasses import replace

import pytest

from codey.knowledge.model import KnowledgeMutation as M
from codey.knowledge.model import KnowledgeObject as K
from codey.knowledge.model import KnowledgeReference as R
from codey.knowledge.store import KnowledgeStore


def example():
    return [
        M(K("sqlite", "entity", "SQLite", "source", title="SQLite", aliases=("嵌入式数据库",))),
        M(K("source", "source", "Under WAL one writer commits at a time.", "source",
            provenance={"url": "https://example.org/wal", "study_id": "experiment-1"})),
        M(K("excerpt", "evidence", "one writer commits at a time", "observed_result",
            provenance={"locator": "paragraph 1"}, references=(R("source", 1, "from_source"),))),
        M(K("claim", "claim", "Writes serialize", "author_interpretation", status="active",
            conditions={"mode": {"state": "reported", "value": "WAL"}, "version": {"state": "not_reported"}},
            references=(R("excerpt", 1, "supported_by"), R("sqlite", 1, "about")))),
        M(K("derived", "inference", "A busy writer may bottleneck", "model_inference",
            references=(R("claim", 1, "depends_on"),))),
    ]


def test_commit_replay_restart_revision_and_pinned_dependencies(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        assert store.repository.commit("first", example()) == {m.value.id: 1 for m in example()}
        assert store.repository.commit("first", example())["claim"] == 1
        with pytest.raises(ValueError, match="different content"):
            store.repository.commit("first", [M(replace(example()[0].value, content="Changed"))])
    finally:
        store.close()
    store = KnowledgeStore(tmp_path)
    try:
        repo = store.repository
        assert repo.read("claim").value.conditions["version"]["state"] == "not_reported"
        repo.commit("revision", [M(replace(repo.read("claim").value, status="withdrawn"), 1)])
        assert repo.read("derived").needs_review
        assert repo.read("claim", 1).value.status == "active"
        with pytest.raises(ValueError, match="revision conflict"):
            repo.commit("stale-editor", [M(example()[3].value, 1)])
    finally:
        store.close()


def test_batch_validation_rolls_back_all_objects(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        rows = example()
        rows[2] = M(replace(rows[2].value, content="Fabricated result"))
        with pytest.raises(ValueError, match="excerpt"):
            store.repository.commit("bad", rows)
        assert store.repository.read("sqlite") is None
        assert store.repository.read("claim") is None
    finally:
        store.close()


def test_answers_and_proximity_cannot_be_supporting_evidence(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        store.repository.commit("prior", [M(K("answer", "answer", "Old answer", "model_inference"))])
        with pytest.raises(ValueError, match="invalid evidence relation"):
            store.repository.commit("circular", [M(K("claim", "claim", "Self reinforcement", "model_inference",
                status="active", references=(R("answer", 1, "supported_by"),)))])
    finally:
        store.close()


def test_scopes_cannot_leak_through_dependencies_and_privacy_purge_is_recursive(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        private = K("private", "observation", "Personal detail", "user_statement", scope="personal", scope_id="owner")
        store.repository.commit("private", [M(private)])
        public = K("public", "inference", "Conclusion", "model_inference", references=(R("private", 1, "depends_on"),))
        with pytest.raises(ValueError, match="restricted scope"):
            store.repository.commit("leak", [M(public)])
        store.repository.commit("derived", [M(replace(public, scope="personal", scope_id="owner"))])
        assert store.repository.purge(["private"]) == {"private", "public"}
        assert store.repository.read("private", 1) is None
        assert store.repository.read("public") is None
    finally:
        store.close()
