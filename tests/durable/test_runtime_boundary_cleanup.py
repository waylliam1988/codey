from __future__ import annotations

from pathlib import Path

from codey.app.context import AppContext


def test_app_context_runtime_adapters_have_one_explicit_owner(tmp_path: Path) -> None:
    state = AppContext(tmp_path / "state", start_scheduler=False)
    try:
        adapters = state.runtime_adapters
        assert adapters.durable is state.durable
        assert adapters.mutations is state.runtime_mutations
        assert adapters.operations is state.runtime_operations
        assert adapters.effects is state.runtime_effects
        assert adapters.delivery is state.tool_result_delivery
    finally:
        state.close()


def test_ui_state_does_not_persist_transcript_messages(tmp_path: Path) -> None:
    state = AppContext(tmp_path / "state", start_scheduler=False)
    try:
        state.save_ui_state(
            {
                "active_id": "chat",
                "sessions": [{"id": "chat", "messages": [{"role": "user", "content": "secret"}]}],
            },
            base_revision=0,
        )
        stored = state.load_ui_state()
        assert "messages" not in stored["sessions"][0]
        assert "secret" not in (tmp_path / "state" / "ui-state.json").read_text(encoding="utf-8")
    finally:
        state.close()


def test_production_code_does_not_import_runtime_jsonl_adapter() -> None:
    root = Path(__file__).parents[2] / "codey"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path.as_posix().endswith("runtime/log/session_log.py"):
            continue
        source = path.read_text(encoding="utf-8")
        if "codey.runtime.log.session_log" in source or "RuntimeSessionLog" in source:
            offenders.append(path.relative_to(root).as_posix())
    assert offenders == []
