"""Delete failures keep the chat discoverable and cleanup can be retried."""

from unittest.mock import Mock

import pytest

from codey.app.api import new_chat_response
from codey.app.context import AppContext


@pytest.fixture
def app(tmp_path):
    state = AppContext(tmp_path, start_scheduler=False)
    state.save_ui_state({"sessions": [{"id": "parent", "title": "parent"},
                                      {"id": "other", "title": "other"}], "active_id": "parent"}, base_revision=0)
    for scope in ("parent", "other"):
        state.durable.create_conversation(scope)
        state.durable.append_message(scope, "user_message", {"text": f"{scope} content"})
    yield state
    state.close()


def session_ids(app):
    return [row["id"] for row in app.load_ui_state()["sessions"]]


def test_parent_conflict_preserves_ui_cache_outputs_and_fork(app, monkeypatch):
    entry = app.durable.store.head("parent")[0]
    app.durable.fork("parent", entry, child_conversation_id="child")
    cache = app.conversation_for("parent")
    cleanup = Mock()
    monkeypatch.setattr(app.execution_outputs, "delete_session", cleanup)
    before = app.load_ui_state()

    status, result = new_chat_response(app, {"session_id": "parent", "preserve_ui": False})

    assert status == 409 and result["ok"] is False
    assert "forked children" in result["error"]
    assert app.load_ui_state() == before
    assert app.conversation_for("parent") is cache
    assert app.durable.has_conversation("parent")
    assert app.durable.has_conversation("child")
    cleanup.assert_not_called()


def test_durable_failure_stops_projection_cleanup(app, monkeypatch):
    monkeypatch.setattr(app.durable, "delete_conversation", Mock(side_effect=OSError("storage unavailable")))
    cleanup = Mock()
    monkeypatch.setattr(app.execution_outputs, "delete_session", cleanup)
    status, result = new_chat_response(app, {"session_id": "parent", "preserve_ui": False})
    assert status == 503 and result["ok"] is False
    assert result["unpurged_stores"] == ["durable"]
    assert session_ids(app) == ["parent", "other"]
    assert app.durable.has_conversation("parent")
    cleanup.assert_not_called()


def test_partial_file_cleanup_keeps_ui_and_can_retry_after_restart(app, tmp_path, monkeypatch):
    outputs = app.managed_outputs
    files = {scope: outputs.write_json_artifact(session_id=scope, run_id="task", payload={"private": scope})
             for scope in ("parent", "other")}
    paths = {scope: outputs._handle_path(scope, "task", ref.handle, ".json") for scope, ref in files.items()}
    for scope in ("parent", "other"):
        app.execution_outputs.create(scope, session_id=scope, run_id="task")
        app.execution_outputs.append_text(scope, "stdout", f"private-{scope}")
    remove = outputs.delete_session
    monkeypatch.setattr(outputs, "delete_session", Mock(side_effect=PermissionError("file locked")))

    status, result = new_chat_response(app, {"session_id": "parent", "preserve_ui": False})
    assert status == 503 and result["ok"] is False
    assert result["unpurged_stores"] == ["managed_outputs"]
    assert session_ids(app) == ["parent", "other"]
    assert not app.durable.has_conversation("parent")
    assert paths["parent"].is_file()
    with pytest.raises(KeyError):
        app.execution_outputs.snapshot("parent")
    monkeypatch.setattr(outputs, "delete_session", remove)
    app.close()

    reopened = AppContext(tmp_path, start_scheduler=False)
    try:
        status, result = new_chat_response(reopened, {"session_id": "parent", "preserve_ui": False})
        assert status == 200 and result["ok"] is True
        assert session_ids(reopened) == ["other"]
        assert not paths["parent"].exists()
        assert paths["other"].is_file()
        assert reopened.execution_outputs.snapshot("other")["stdout"]["text"] == "private-other"
        assert reopened.durable.has_conversation("other")
        status, result = new_chat_response(reopened, {"session_id": "parent", "preserve_ui": False})
        assert status == 200 and result["ok"] is True
    finally:
        reopened.close()


@pytest.mark.parametrize("failure", ["approvals", "events", "ui_state"])
def test_cleanup_errors_are_reported_and_keep_sidebar_for_retry(app, monkeypatch, failure):
    if failure == "ui_state":
        owner, name = app.ui_state_store, "forget_session"
    else:
        owner = app.conversation_coordinator
        name = "_expire_approvals" if failure == "approvals" else "_flush_events"
    original = getattr(owner, name)
    monkeypatch.setattr(owner, name, Mock(side_effect=OSError("cleanup unavailable")))
    app.execution_outputs.create("execution", session_id="parent", run_id="task")
    status, result = new_chat_response(app, {"session_id": "parent", "preserve_ui": False})
    assert status == 503 and result["ok"] is False
    assert result["unpurged_stores"] == [failure]
    assert session_ids(app) == ["parent", "other"]
    with pytest.raises(KeyError):
        app.execution_outputs.snapshot("execution")
    monkeypatch.setattr(owner, name, original)
    status, result = new_chat_response(app, {"session_id": "parent", "preserve_ui": False})
    assert status == 200 and result["ok"] is True
    assert session_ids(app) == ["other"]
