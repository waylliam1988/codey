"""Strict schema_version locks (red-first, cold-start v1 follow-up).

Prior rounds made operation_state / local_config / profiles / prompt_surface
strict with ``type(x) is int`` so ``True``/``1.0``/``"1"`` never pass as ``1``.
Remaining readers still use bare ``!=`` which accepts ``True`` (``True == 1``)
and ``1.0`` as v1. These tests assert the CLEANED strict state, so they fail
on pre-fix code and pass after.
"""
from __future__ import annotations

import json
from pathlib import Path


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_revision_rejects_bool_and_float_schema(tmp_path: Path) -> None:
    from codey.workspace.revision import WorkspaceRevisionStore

    store = WorkspaceRevisionStore(tmp_path)
    proj = "demo-proj"
    path = store.path_for(proj)
    for bad in (True, 1.0, "1", 2, 0):
        _write(path, {"schema_version": bad, "revision": 5})
        try:
            store._read_revision_unlocked(path)
        except Exception:
            continue  # fail-closed via exception is acceptable
        else:
            # if it returns instead of raising, it must not return the stored 5
            # (True/1.0 must not pass as v1)
            result = store._read_revision_unlocked(path)
            assert result != 5, f"schema_version={bad!r} passed as v1"
    # valid v1 still works
    _write(path, {"schema_version": 1, "revision": 5})
    assert store._read_revision_unlocked(path) == 5


def test_facts_rejects_bool_schema(tmp_path: Path) -> None:
    from codey.workspace.facts import ProjectFactsStore

    store = ProjectFactsStore(tmp_path)
    proj = "demo-proj"
    path = store.path_for(proj)
    _write(
        path,
        {
            "schema_version": True,
            "commands": [{"command": "echo hi", "cwd": "."}],
            "successful_changes": [],
        },
    )
    loaded = store.load(proj)
    assert len(loaded.commands) == 0, "True schema must not load commands"


def test_conversation_rejects_bool_schema(tmp_path: Path) -> None:
    from codey.storage.conversation_store import ConversationStore

    store = ConversationStore(tmp_path)
    path = store.path_for("sess-1")
    _write(path, {"schema_version": True, "used_tokens": 123})
    loaded = store.load("sess-1")
    assert loaded.used_tokens == 0, "True schema must fail closed to default"


def test_ui_state_rejects_bool_schema(tmp_path: Path) -> None:
    from codey.storage.ui_state_store import UiStateStore

    store = UiStateStore(tmp_path)
    _write(store.path, {"schema_version": True, "state": {"revision": 7}})
    loaded = store.load()
    assert loaded.get("revision") == 0, "True schema must fail closed to empty"


def test_strict_source_present_in_all_readers() -> None:
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "codey"
    loose_files = [
        "workspace/revision.py",
        "workspace/facts.py",
        "workspace/changes.py",
        "workspace/config.py",
        "storage/conversation_store.py",
        "storage/ui_state_store.py",
        "runs/ledger.py",
        "runs/ledger_projection.py",
        "runs/details.py",
        "runs/receipt.py",
        "runs/work_checkpoint.py",
        "ghost/continuity.py",
        "ghost/directive.py",
        "ghost/inbox.py",
        "ghost/sleep.py",
        "ghost/work_queue.py",
        "ghost/event_log.py",
        "ghost/event_projection.py",
        "research/evidence_ledger.py",
        "runtime/effects/tool_result_delivery.py",
        "runtime/effects/effect_records.py",
        "runtime/log/entries.py",
    ]
    missing: list[str] = []
    for rel in loose_files:
        text = (root / rel).read_text(encoding="utf-8")
        # must contain a strict type-is-int guard for schema_version
        has_strict = ("type(" in text) and ("is not int" in text) and ("schema_version" in text)
        if not has_strict:
            missing.append(rel)
    assert not missing, f"missing strict schema_version guard in: {missing}"
