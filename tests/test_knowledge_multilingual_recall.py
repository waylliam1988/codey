from dataclasses import replace

from codey.knowledge.model import KnowledgeMutation as M
from codey.knowledge.model import KnowledgeObject as K
from codey.knowledge.model import KnowledgeReference as R
from codey.knowledge.recall import KnowledgeAccess, KnowledgeHit, KnowledgeRecall, render_recall
from codey.knowledge.search_text import SEARCH_INDEX_VERSION, query_terms
from codey.knowledge.store import KnowledgeStore

QUERY = (
    "检索刚才关于队列唯一闸门的候选推论，读取其当前版本。我们尚未测量实际队列的工作负载，"
    "请保留其身份、内容和已有引用，把 conditions 中 workload 标为 not_extracted，"
    "并增加 provenance 中 interpretation='analogy_requires_queue_measurement'。"
    "用 knowledge_commit 提交修订，expected_revision 必须来自读取结果。不要编造实验，最后说明这只是适用条件修订。"
)


def populate(store):
    store.repository.commit(
        "premises",
        [
            M(K("reader", "claim", "WAL 下读者与写者可以并发。", "model_inference")),
            M(K("writer", "claim", "WAL 同时只能有一个写者。", "model_inference")),
            M(
                K(
                    "source",
                    "source",
                    "Conditions workload not extracted. " * 300,
                    "source",
                    provenance={"url": "https://sqlite.org/wal.html"},
                )
            ),
        ],
    )
    value = K(
        "queue-inference",
        "inference",
        "队列的唯一闸门可能导致串行写入瓶颈，仍需验证。",
        "model_inference",
        references=(R("writer", 1, "depends_on"),),
    )
    store.repository.commit("candidate", [M(value)])
    return value


def test_chinese_revision_target_survives_english_distractor_and_background_budget(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        populate(store)
        hits = store.search(QUERY)
        assert hits[0].version.value.id == "queue-inference"
        background = render_recall(hits, max_chars=3000)
        assert '"id":"queue-inference"' in background
        assert "not evidence strength" in background
        # Derived bigrams must not become semantic aliases or knowledge content.
        assert store.repository.read("queue-inference").value.aliases == ()
        assert len(query_terms(QUERY)) <= 76
    finally:
        store.close()


def test_chinese_scope_filter_precedes_limit(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        value = populate(store)
        for i in range(30):
            store.repository.commit(
                f"private-{i}",
                [
                    M(
                        replace(
                            value, id=f"private-{i}", scope="project", scope_id="hidden", title="队列唯一闸门候选推论"
                        )
                    )
                ],
            )
        hits = KnowledgeRecall(store.repository).search(
            QUERY, access=KnowledgeAccess(project="visible"), limit=2, depth=0
        )
        assert any(hit.version.value.id == value.id for hit in hits)
        assert all(hit.version.value.scope == "global" for hit in hits)
    finally:
        store.close()


def test_existing_index_upgrade_preserves_versions_and_commit_receipts(tmp_path):
    store = KnowledgeStore(tmp_path)
    value = populate(store)
    before = store.repository.read(value.id).to_dict()
    with store.index.transaction() as conn:
        conn.execute("DELETE FROM knowledge_meta WHERE key='structured_fts_version'")
        conn.execute("DELETE FROM knowledge_fts")
        conn.execute("INSERT INTO knowledge_fts VALUES(?,?,?,?)", (value.id, "", value.content, ""))
    store.close()
    store = KnowledgeStore(tmp_path)
    try:
        assert store.repository.read(value.id).to_dict() == before
        assert store.repository.committed("candidate", [M(value)]) == {value.id: 1}
        assert store.search(QUERY)[0].version.value.id == value.id
        assert (
            store.index._read_conn()
            .execute("SELECT value FROM knowledge_meta WHERE key='structured_fts_version'")
            .fetchone()[0]
            == SEARCH_INDEX_VERSION
        )
    finally:
        store.close()


def test_oversized_recall_row_does_not_hide_later_small_candidate(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        value = populate(store)
        large = replace(value, id="large", provenance={"detail": "x" * 6000})
        store.repository.commit("large", [M(large)])
        hits = [
            KnowledgeHit(store.repository.read("large"), "text match"),
            KnowledgeHit(store.repository.read(value.id), "text match"),
        ]
        text = render_recall(hits, max_chars=2000)
        assert len(text) <= 2000
        assert '"id":"queue-inference"' in text
        assert '"id":"large"' not in text
    finally:
        store.close()
