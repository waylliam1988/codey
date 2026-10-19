from __future__ import annotations

from pathlib import Path

from codey.app.context import AppContext


def _request(run_id: str) -> dict[str, object]:
    return {
        "session_id": "chat",
        "run_id": run_id,
        "task": "hello durable input",
        "provider": "local",
        "intent": "chat",
        "max_turns": 8,
    }


def test_app_input_is_a_durable_submission_without_legacy_input_database(tmp_path: Path) -> None:
    root = tmp_path / "state"
    run_id = "run_" + "a" * 32
    state = AppContext(root, start_scheduler=False)
    try:
        item = state.input_queue.create("input-one", _request(run_id))
        assert item["state"] == "queued"
        assert state.durable.task(run_id).state["input_identity"] == "input-one"
        assert len(state.durable.entries("chat")) == 1
        assert not (root / "conversation_inputs.sqlite3").exists()
    finally:
        state.close()


def test_durable_input_projection_survives_app_restart(tmp_path: Path) -> None:
    root = tmp_path / "state"
    run_id = "run_" + "b" * 32
    state = AppContext(root, start_scheduler=False)
    state.input_queue.create("input-one", _request(run_id))
    state.close()

    restarted = AppContext(root, start_scheduler=False)
    try:
        item = restarted.input_queue.get("input-one")
        assert item is not None
        assert item["state"] == "queued"
        assert item["request"]["task"] == "hello durable input"
        assert len(restarted.durable.entries("chat")) == 1
    finally:
        restarted.close()
