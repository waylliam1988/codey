from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codey.ghost.observations import GhostObservationStore
from codey.operations.ghost_context import ghost_experiences


class Embeddings:
    model_id = "test-semantic-space"
    dimensions = 3

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def encode(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.calls.append(texts)
        return tuple((1.0, 0.0, 0.0) if "feline" in text or "cat" in text else (0.0, 1.0, 0.0)
                     for text in texts)


def append(store: GhostObservationStore, identity: str, text: str, *, session: str = "s", reason: str = "done") -> None:
    assert store.append_completed(run_id=identity, session_id=session, project="", mode="chat",
                                  user_text=text, assistant_text="recorded experience", stop_reason=reason)


def test_vector_retrieval_uses_native_sqlite_vec_and_preserves_scope_and_budget(tmp_path: Path) -> None:
    from types import SimpleNamespace

    embedder = Embeddings()
    store = GhostObservationStore(tmp_path, embeddings=embedder)
    append(store, "cat", "cat feeding routine")
    append(store, "other", "database locking")
    append(store, "foreign", "cat secrets", session="another")
    append(store, "failed", "cat failed operation", reason="error")
    state = SimpleNamespace(
        ghost_observations=store,
        ghost_inbox=SimpleNamespace(
            control_enabled=lambda _name, **_kwargs: True,
        ),
    )

    context = ghost_experiences(state, query="feline care", session_id="s", max_items=1, budget_chars=400)
    assert "cat feeding routine" in context
    assert "database locking" not in context and "cat secrets" not in context and "cat failed operation" not in context
    assert len(context) <= 400
    assert not (tmp_path / "durable.sqlite3").exists()
    with store.database.connection() as database:
        assert database.execute("SELECT count(*) FROM memory_embeddings").fetchone()[0] == 2
    calls = len(embedder.calls)
    ghost_experiences(state, query="feline care", session_id="s", max_items=1)
    assert len(embedder.calls) == calls + 1, "unchanged observations must reuse their persisted vectors"


def test_rewritten_observation_invalidates_old_embedding_and_deletion_cascades(tmp_path: Path) -> None:
    store = GhostObservationStore(tmp_path, embeddings=Embeddings())
    append(store, "one", "cat routine")
    store.retrieve("feline", session_id="s")
    with sqlite3.connect(tmp_path / "ghost.sqlite3") as database:
        old = database.execute("SELECT content_digest FROM memory_embeddings").fetchone()[0]
    append(store, "one", "database maintenance")
    store.retrieve("database", session_id="s")
    with sqlite3.connect(tmp_path / "ghost.sqlite3") as database:
        assert database.execute("SELECT content_digest FROM memory_embeddings").fetchone()[0] != old
    store.delete_scope("session", session_id="s")
    with sqlite3.connect(tmp_path / "ghost.sqlite3") as database:
        assert database.execute("SELECT count(*) FROM memory_embeddings").fetchone()[0] == 0


@pytest.mark.parametrize("vector", [(float("nan"), 0.0, 1.0), (0.0, 0.0, 0.0), (1.0, 0.0)])
def test_invalid_embedding_is_rejected_before_writing(tmp_path: Path, vector: tuple[float, ...]) -> None:
    class Invalid(Embeddings):
        def encode(self, texts):
            return tuple(vector for _text in texts)

    store = GhostObservationStore(tmp_path, embeddings=Invalid())
    append(store, "one", "cat routine")
    with pytest.raises(ValueError, match="embedding"):
        store.retrieve("feline", session_id="s")
    with store.database.connection() as database:
        assert database.execute("SELECT count(*) FROM memory_embeddings").fetchone()[0] == 0


def test_configured_endpoint_is_used_by_the_production_retrieval_path(tmp_path: Path, monkeypatch) -> None:
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            data = [{"index": index, "embedding": [1.0, 0.0, 0.0]}
                    for index in reversed(range(len(request["input"])))]
            body = json.dumps({"data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("GHOST_EMBEDDING_URL", f"http://127.0.0.1:{httpd.server_port}/embeddings")
    monkeypatch.setenv("GHOST_EMBEDDING_MODEL", "local-test")
    monkeypatch.setenv("GHOST_EMBEDDING_DIMENSIONS", "3")
    try:
        store = GhostObservationStore(tmp_path)
        append(store, "one", "cat routine")
        rendered = ghost_experiences(
            SimpleNamespace(
                ghost_observations=store,
                ghost_inbox=SimpleNamespace(
                    control_enabled=lambda _name, **_kwargs: True,
                ),
            ),
                                     query="feline care", session_id="s")
        assert "cat routine" in rendered
        assert len(requests) == 2
        assert all(request["model"] == "local-test" for request in requests)
        reopened = GhostObservationStore(tmp_path)
        assert reopened.retrieve("feline", session_id="s")
        assert len(requests) == 3, "restart must reuse the persisted observation vector"
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=5)
