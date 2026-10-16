from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path


def test_transcript_store_paginates_deduplicates_and_respects_epoch(tmp_path: Path) -> None:
    from codey.storage.transcript_store import TranscriptStore

    store = TranscriptStore(tmp_path)
    store.append("s1", {"id": "m1", "type": "user", "text": "one"}, message_id="m1", event_id="e1")
    store.append("s1", {"id": "m2", "type": "asst", "text": "two"}, message_id="m2", event_id="e2")
    store.append("s1", {"id": "m2", "type": "asst", "text": "two updated"}, message_id="m2", event_id="e2")
    page = store.page("s1", limit=1)
    assert [row["id"] for row in page["items"]] == ["m2"]
    assert page["has_more"] is True
    older = store.page("s1", before=page["before_cursor"], limit=10)
    assert [row["id"] for row in older["items"]] == ["m1"]
    epoch = store.clear("s1")
    assert store.append("s1", {"id": "old", "text": "late"}, message_id="old", epoch="old") is False
    assert store.append("s1", {"id": "m1", "type": "user", "text": "late old"}, message_id="m1") is False
    assert store.page("s1", limit=10)["epoch"] == epoch


def test_app_context_uses_one_archive_identity_for_stream_updates(tmp_path: Path) -> None:
    from codey.app.context import AppContext

    ctx = AppContext(tmp_path / "state")
    try:
        state = {"revision": 0, "active_id": "s1", "sessions": [{"id": "s1", "messages": [{
            "type": "asst", "streamId": "stream-1", "streamState": "partial", "text": "part 1",
            "eventKey": "reply-stream:1",
        }]}], "projects": []}
        saved = ctx.save_ui_state(state, base_revision=0)
        state["revision"] = saved["revision"]
        state["sessions"][0]["messages"][0].update(text="part 2", streamState="complete", eventKey="reply-stream:2")
        ctx.save_ui_state(state, base_revision=saved["revision"])
        items = ctx.transcript_store.page("s1", limit=10)["items"]
        assert [(item.get("text"), item.get("streamId")) for item in items] == [("part 2", "stream-1")]
    finally:
        ctx.close()


def test_transcript_store_search_is_literal_and_bounded(tmp_path: Path) -> None:
    from codey.storage.transcript_store import TranscriptStore

    store = TranscriptStore(tmp_path)
    for index in range(10):
        store.append("s1", {"id": f"m{index}", "type": "asst", "text": f"cache {index}"}, message_id=f"m{index}")
    hits = store.search("s1", "cache", limit=3)
    assert len(hits) == 3
    assert hits[0]["message_id"] == "m0"


def test_ui_baseline_import_keeps_messages_beyond_visible_window(tmp_path: Path) -> None:
    from codey.app.context import AppContext

    ctx = AppContext(tmp_path / "state")
    try:
        messages = [{"id": f"m{i}", "type": "user", "text": str(i)} for i in range(600)]
        saved = ctx.save_ui_state({"revision": 0, "active_id": "s", "sessions": [{"id": "s", "messages": messages}], "projects": []}, base_revision=0)
        assert len(saved["sessions"][0]["messages"]) == 500
        page = ctx.transcript_store.page("s", limit=200)
        assert page["items"][0]["id"] == "m400"
        assert page["items"][-1]["id"] == "m599"
        seen = [item["id"] for item in page["items"]]
        while page["has_more"]:
            page = ctx.transcript_store.page("s", before=page["before_cursor"], limit=200)
            seen = [item["id"] for item in page["items"]] + seen
        assert seen[0] == "m0"
        assert seen[-1] == "m599"
        assert ctx.transcript_store.search("s", "5", limit=100)
    finally:
        ctx.close()


def test_forget_clears_ui_projection_as_well_as_archive(tmp_path: Path) -> None:
    from codey.app.context import AppContext

    ctx = AppContext(tmp_path / "state")
    try:
        saved = ctx.save_ui_state({"revision": 0, "active_id": "s", "sessions": [{"id": "s", "messages": [{"id": "m", "type": "user", "text": "x"}]}], "projects": []}, base_revision=0)
        assert saved["sessions"]
        assert not ctx.forget_conversation("s")
        assert not ctx.load_ui_state()["sessions"]
        assert not ctx.transcript_store.page("s", limit=10)["items"]
    finally:
        ctx.close()


def test_new_chat_reset_preserves_new_session_ui_projection(tmp_path: Path) -> None:
    from codey.app.api import new_chat_response
    from codey.app.context import AppContext

    ctx = AppContext(tmp_path / "state")
    try:
        ctx.save_ui_state({"revision": 0, "active_id": "new", "sessions": [{"id": "new", "messages": []}], "projects": []}, base_revision=0)
        status, payload = new_chat_response(ctx, {"session_id": "new", "preserve_ui": True})
        assert status == 200 and payload["ok"] is True
        assert any(row["id"] == "new" for row in ctx.load_ui_state()["sessions"])
    finally:
        ctx.close()


def test_execution_output_store_keeps_bounded_snapshots(tmp_path: Path) -> None:
    from codey.storage.execution_output_store import ExecutionOutputStore

    store = ExecutionOutputStore(tmp_path, max_bytes=8)
    store.create("e1", session_id="s1", run_id="r1")
    store.append("e1", "stdout", b"1234")
    store.append("e1", "stdout", "前".encode() + b"56789")
    snapshot = store.snapshot("e1")
    assert snapshot["execution_id"] == "e1"
    assert snapshot["stdout"]["total_bytes"] > len(snapshot["stdout"]["text"].encode())
    store.settled("e1", 0)
    assert store.snapshot("e1")["execution_state"] == "settled"


def test_transcript_store_concurrent_append_allocates_unique_ordinals(tmp_path: Path) -> None:
    from codey.storage.transcript_store import TranscriptStore

    store = TranscriptStore(tmp_path)

    def append(index: int) -> bool:
        return store.append("concurrent", {"id": f"m{index}", "type": "user", "text": str(index)}, message_id=f"m{index}")

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(append, range(100)))
    assert all(results)
    page = store.page("concurrent", limit=200)
    assert len(page["items"]) == 100
    assert [item["ordinal"] for item in page["items"]] == list(range(1, 101))


def test_stale_ui_save_cannot_archive_new_messages_after_clear(tmp_path: Path) -> None:
    from codey.app.context import AppContext

    ctx = AppContext(tmp_path / "state")
    try:
        saved = ctx.save_ui_state(
            {"revision": 0, "active_id": "s", "sessions": [{"id": "s", "messages": [{"id": "old", "type": "user", "text": "old"}]}], "projects": []},
            base_revision=0,
        )
        ctx.forget_conversation("s")
        stale = {"revision": saved["revision"], "active_id": "s", "sessions": [{"id": "s", "messages": [{"id": "late", "type": "user", "text": "late"}]}], "projects": []}
        with suppress(ValueError):
            ctx.save_ui_state(stale, base_revision=saved["revision"])
        assert ctx.transcript_store.search("s", "late") == []
    finally:
        ctx.close()
