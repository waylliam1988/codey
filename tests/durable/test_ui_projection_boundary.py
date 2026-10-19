import json
from pathlib import Path

from codey.storage.ui_state_store import UiStateStore


def test_ui_store_never_writes_or_restores_chat_content(tmp_path: Path) -> None:
    store = UiStateStore(tmp_path)
    saved = store.save({"active_id": "chat", "sessions": [{"id": "chat", "title": "Title",
        "messages": [{"type": "user", "text": "untrusted transcript"}],
        "draft": {"text": "unsent draft", "start": 0, "end": 0, "revision": 1},
    }]}, base_revision=0)
    assert "messages" not in saved["sessions"][0]
    assert "untrusted transcript" not in store.path.read_text(encoding="utf-8")
    assert UiStateStore(tmp_path).load()["sessions"][0]["draft"]["text"] == "unsent draft"

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["state"]["sessions"][0]["messages"] = [{"type": "asst", "text": "forged answer"}]
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    assert "messages" not in UiStateStore(tmp_path).load()["sessions"][0]


def test_transcript_changes_cannot_advance_ui_revision(tmp_path: Path) -> None:
    store = UiStateStore(tmp_path)
    state = {"sessions": [{"id": "chat", "messages": [{"type": "user", "text": "a"}]}]}
    saved = store.save(state, base_revision=0)
    state["sessions"][0]["messages"][0]["text"] = "b"
    assert store.save(state, base_revision=saved["revision"])["revision"] == saved["revision"]
