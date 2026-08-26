from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from codey.runtime import cancellation
from codey.providers import controls as provider_controls
from codey.ghost import router as router_module
from codey.ghost.router import (
    GhostRouteDecision,
    GhostRouteRequest,
    GhostRouteStore,
    GhostRouter,
    finalize_route_decision,
    parse_route_reply,
    render_route_prompt,
)


class _Provider:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.new_chat_called = False
        self.closed = False
        self.prompts: list[str] = []

    def new_chat(self, timeout: float | None = None) -> None:
        del timeout
        self.new_chat_called = True

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout
        self.prompts.append(text)
        return self.reply

    def close(self) -> None:
        self.closed = True


class _CancelProvider(_Provider):
    def send(self, text: str, timeout: float | None = None) -> str:
        del text, timeout
        raise cancellation.TaskCancelled("task stopped")


class _TeachCancelProvider(_Provider):
    def send(self, text: str, timeout: float | None = None) -> str:
        del text, timeout
        raise provider_controls.ControlTeachCancelled("teach cancelled")


def _request(**kwargs) -> GhostRouteRequest:
    data = {
        "task": "帮我查一下今天的 pytest 推荐写法",
        "baseline_mode": "chat",
        "run_id": "run-1",
        "session_id": "session-1",
        "project": "",
        "provider_id": "deepseek",
        "continue_request": False,
        "has_reviewable_diff": False,
    }
    data.update(kwargs)
    return GhostRouteRequest(**data)


def _accepted_route_result(request: GhostRouteRequest) -> router_module.GhostRouteResult:
    return finalize_route_decision(
        request,
        GhostRouteDecision("research", 0.92, "fresh", True),
    )


def _store_with_stale_projection(
    state_home: str,
    monkeypatch: pytest.MonkeyPatch,
) -> GhostRouteStore:
    store = GhostRouteStore(state_home)
    original_write = router_module.write_json_atomic
    block_state = {"enabled": False}

    def flaky_write(path, *args, **kwargs) -> None:
        if block_state["enabled"] and Path(path).name == "router_state.json":
            raise OSError("state unavailable")
        original_write(path, *args, **kwargs)

    monkeypatch.setattr(router_module, "write_json_atomic", flaky_write)
    request_1 = _request(session_id="session-1", task="first")
    assert store.append_result(_accepted_route_result(request_1), request_1)
    block_state["enabled"] = True
    request_2 = _request(session_id="session-2", task="second")
    assert store.append_result(_accepted_route_result(request_2), request_2)
    assert "router_state_write_failed" in store.last_warnings
    block_state["enabled"] = False
    return store


def test_prompt_is_bounded_and_has_no_internal_names() -> None:
    prompt = render_route_prompt(_request(task="x" * 2_000, project="E:/codey"))

    assert "Codey" not in prompt
    assert "Ghost" not in prompt
    assert "Local Context" not in prompt
    assert len(prompt) < 2_500
    assert "x" * 1_200 not in prompt


def test_parse_reply_accepts_fenced_json_and_aliases() -> None:
    decision = parse_route_reply(
        '```json\n{"mode":"project_writer","confidence":0.91,"reason":"edit files"}\n```'
    )

    assert decision.parse_ok
    assert decision.mode == "project"
    assert decision.confidence == 0.91


def test_parse_reply_rejects_multiple_top_level_json_objects() -> None:
    decision = parse_route_reply(
        '{"mode":"project_writer","confidence":0.91}\n'
        '{"mode":"chat","confidence":0.99}'
    )

    assert not decision.parse_ok
    assert decision.mode == ""
    assert decision.diagnostics == ("too_many_json_objects",)


def test_parse_reply_rejects_prose_or_array_wrapped_json() -> None:
    prose = parse_route_reply('Sure: {"mode":"research","confidence":0.91}')
    array = parse_route_reply('[{"mode":"research","confidence":0.91}]')

    assert not prose.parse_ok
    assert prose.diagnostics == ("json_not_top_level_object",)
    assert not array.parse_ok
    assert array.diagnostics == ("json_not_top_level_object",)


def test_low_confidence_falls_back_to_baseline() -> None:
    result = finalize_route_decision(
        _request(baseline_mode="chat"),
        GhostRouteDecision("research", 0.4, "not sure", True),
    )

    assert result.final_mode == "chat"
    assert result.skipped_reason == "low_confidence"
    assert not result.accepted


def test_local_policy_blocks_writer_when_user_says_not_to_edit() -> None:
    result = finalize_route_decision(
        _request(
            baseline_mode="project",
            project="E:/codey",
            task="先别改代码，只给我一个方案",
        ),
        GhostRouteDecision("project", 0.95, "project task", True),
    )

    assert result.final_mode == "planning_readonly"
    assert result.skipped_reason == "edit_forbidden"


@pytest.mark.parametrize(
    ("task", "selected", "expected"),
    [
        ("不要读写项目文件，只普通聊一下。", "project", "chat"),
        ("不要读项目文件，只聊天。", "planning_readonly", "chat"),
        ("不访问项目文件，直接回答。", "project", "chat"),
        ("不读取源码，直接回答。", "project", "chat"),
        ("不看代码，直接回答。", "project", "chat"),
        ("do not read or write files; just answer generally", "project", "chat"),
        ("not read project files; just answer generally", "project", "chat"),
        ("without reading files, explain the idea", "review", "chat"),
        ("不要读写项目文件，但查一下今天的 pytest 变化。", "hybrid", "research"),
    ],
)
def test_local_policy_blocks_project_access_when_user_forbids_file_access(
    task: str,
    selected: str,
    expected: str,
) -> None:
    result = finalize_route_decision(
        _request(
            baseline_mode="project",
            project="E:/codey",
            task=task,
            has_reviewable_diff=True,
        ),
        GhostRouteDecision(selected, 0.95, "forced", True),
    )

    assert result.final_mode == expected
    assert result.skipped_reason == "project_access_forbidden"


@pytest.mark.parametrize(
    "task",
    [
        "Normalize file reading code in the parser.",
        "Implement no-op handling in file reading code.",
    ],
)
def test_local_policy_does_not_treat_word_fragments_as_project_access_denial(task: str) -> None:
    result = finalize_route_decision(
        _request(
            baseline_mode="project",
            project="E:/codey",
            task=task,
        ),
        GhostRouteDecision("project", 0.95, "forced", True),
    )

    assert result.final_mode == "project"
    assert result.skipped_reason == ""


def test_local_policy_degrades_modes_without_project() -> None:
    hybrid = finalize_route_decision(
        _request(baseline_mode="chat", project="", task="查一下最新文档并更新说明"),
        GhostRouteDecision("hybrid", 0.95, "research and edit", True),
    )
    review = finalize_route_decision(
        _request(baseline_mode="chat", project="", task="review this diff"),
        GhostRouteDecision("review", 0.95, "review", True),
    )

    assert hybrid.final_mode == "research"
    assert hybrid.skipped_reason == "no_project"
    assert review.final_mode == "chat"
    assert review.skipped_reason == "no_project"


def test_review_without_diff_degrades_to_readonly_planning() -> None:
    result = finalize_route_decision(
        _request(
            baseline_mode="project",
            project="E:/codey",
            task="review 一下这次 diff，不要修改",
            has_reviewable_diff=False,
        ),
        GhostRouteDecision("review", 0.95, "review diff", True),
    )

    assert result.final_mode == "planning_readonly"
    assert result.skipped_reason == "no_reviewable_diff"


def test_router_provider_failure_falls_back_to_baseline() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = GhostRouteStore(td)
        router = GhostRouter(store)

        def failing_factory(_provider_id: str):
            raise RuntimeError("offline API_KEY=sk-secret-should-not-persist")

        result = router.route(
            _request(baseline_mode="project"),
            provider_factory=failing_factory,
            max_attempts=1,
        )
        exported = store.export_state()
        raw_events = Path(td, "ghost", "router_events.jsonl").read_text(encoding="utf-8")

    assert result.final_mode == "project"
    assert result.skipped_reason == "router_error"
    assert result.ok is False
    assert result.diagnostics == ("provider_connect_failed:RuntimeError",)
    assert "sk-secret-should-not-persist" not in json.dumps(exported, ensure_ascii=False)
    assert "sk-secret-should-not-persist" not in raw_events


def test_router_retries_transient_provider_exception_once() -> None:
    router = GhostRouter()
    providers = [
        RuntimeError("transient"),
        _Provider('{"mode":"research","confidence":0.92,"reason":"fresh"}'),
    ]

    def factory(_provider_id: str):
        item = providers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    result = router.route(
        _request(baseline_mode="chat"),
        provider_factory=factory,
        max_attempts=2,
    )

    assert result.final_mode == "research"
    assert result.accepted
    assert providers == []


def test_router_cancellation_bubbles_and_closes_provider() -> None:
    provider = _CancelProvider("{}")

    with pytest.raises(cancellation.TaskCancelled):
        GhostRouter().route(
            _request(baseline_mode="chat"),
            provider_factory=lambda _provider_id: provider,
        )

    assert provider.closed


def test_router_control_teach_cancellation_bubbles_and_closes_provider() -> None:
    provider = _TeachCancelProvider("{}")

    with pytest.raises(provider_controls.ControlTeachCancelled):
        GhostRouter().route(
            _request(baseline_mode="chat"),
            provider_factory=lambda _provider_id: provider,
        )

    assert provider.closed


def test_router_audit_failure_keeps_baseline_mode() -> None:
    class FailingStore(GhostRouteStore):
        def append_result(self, result, request) -> bool:
            del result, request
            self.last_warnings = ("router_audit_write_failed",)
            return False

    provider = _Provider('{"mode":"research","confidence":0.92,"reason":"fresh"}')

    result = GhostRouter(FailingStore(tempfile.mkdtemp())).route(
        _request(baseline_mode="chat"),
        provider_factory=lambda _provider_id: provider,
    )

    assert result.final_mode == "chat"
    assert result.selected_mode == "research"
    assert result.skipped_reason == "router_audit_failed"
    assert not result.accepted
    assert "router_audit_write_failed" in result.warnings


def test_router_state_projection_failure_does_not_block_audited_route(monkeypatch) -> None:
    def failing_write(*_args, **_kwargs) -> None:
        raise OSError("projection unavailable")

    with tempfile.TemporaryDirectory() as td:
        store = GhostRouteStore(td)
        monkeypatch.setattr(router_module, "write_json_atomic", failing_write)

        result = GhostRouter(store).route(
            _request(baseline_mode="chat"),
            provider_factory=lambda _provider_id: _Provider(
                '{"mode":"research","confidence":0.92,"reason":"fresh"}'
            ),
        )
        exported = store.export_state()
        raw_events = Path(td, "ghost", "router_events.jsonl").read_text(encoding="utf-8")

    assert result.final_mode == "research"
    assert result.accepted
    assert "router_state_write_failed" in result.warnings
    assert exported["router"]["records"][-1]["final_mode"] == "research"
    assert '"final_mode":"research"' in raw_events


def test_router_append_bootstraps_projection_when_event_log_is_missing() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = GhostRouteStore(td)
        request_1 = _request(session_id="session-1", task="first")
        assert store.append_result(_accepted_route_result(request_1), request_1)
        store.events_path.unlink()
        request_2 = _request(session_id="session-2", task="second")

        assert store.append_result(_accepted_route_result(request_2), request_2)
        exported = store.export_state()
        raw_events = store.events_path.read_text(encoding="utf-8")

    assert {record["session_id"] for record in exported["router"]["records"]} == {
        "session-1",
        "session-2",
    }
    assert "session-1" in raw_events
    assert "session-2" in raw_events


def test_router_append_compaction_uses_events_not_stale_projection(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        store = _store_with_stale_projection(td, monkeypatch)
        monkeypatch.setattr(router_module, "MAX_ROUTER_EVENTS", 2)
        request_3 = _request(session_id="session-3", task="third")

        assert store.append_result(_accepted_route_result(request_3), request_3)
        raw_events = store.events_path.read_text(encoding="utf-8")
        records = store.export_state()["router"]["records"]

    assert "session-2" in raw_events
    assert "session-3" in raw_events
    assert {record["session_id"] for record in records} >= {"session-2", "session-3"}


def test_router_public_compaction_uses_events_not_stale_projection(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        store = _store_with_stale_projection(td, monkeypatch)
        monkeypatch.setattr(router_module, "MAX_ROUTER_EVENTS", 1)

        result = store.compact_if_needed()
        raw_events = store.events_path.read_text(encoding="utf-8")
        records = store.export_state()["router"]["records"]

    assert result["ok"] is True
    assert "session-2" in raw_events
    assert {record["session_id"] for record in records} >= {"session-1", "session-2"}


def test_router_audit_is_bounded_and_does_not_store_task_text() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = GhostRouteStore(td)
        provider = _Provider(
            '{"mode":"research","confidence":0.92,'
            '"reason":"user mentioned API_KEY=sk-secret-should-not-persist"}'
        )
        router = GhostRouter(store)
        request = _request(
            task="帮我查一下 SECRET FULL USER TASK SHOULD NOT BE STORED",
            baseline_mode="chat",
            session_id="session-secret",
        )

        result = router.route(request, provider_factory=lambda _provider_id: provider)
        exported = store.export_state()
        raw_events = Path(td, "ghost", "router_events.jsonl").read_text(encoding="utf-8")

    assert result.final_mode == "research"
    assert provider.new_chat_called
    assert provider.closed
    assert "SECRET FULL USER TASK" not in json.dumps(exported, ensure_ascii=False)
    assert "SECRET FULL USER TASK" not in raw_events
    assert "sk-secret-should-not-persist" not in json.dumps(exported, ensure_ascii=False)
    assert "sk-secret-should-not-persist" not in raw_events
    assert exported["router"]["records"][-1]["reason"] == "accepted"
    assert "task_hash" in raw_events
    assert "task_chars" in raw_events


def test_router_delete_scope_removes_matching_session_records() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = GhostRouteStore(td)
        router = GhostRouter(store)
        router.route(
            _request(session_id="session-delete"),
            provider_factory=lambda _provider_id: _Provider('{"mode":"research","confidence":0.9,"reason":"fresh"}'),
        )
        router.route(
            _request(session_id="session-keep"),
            provider_factory=lambda _provider_id: _Provider('{"mode":"research","confidence":0.9,"reason":"fresh"}'),
        )

        removed = store.delete_scope("session", session_id="session-delete")
        records = store.export_state()["router"]["records"]

    assert removed == 1
    assert len(records) == 1
    assert records[0]["session_id"] == "session-keep"


def test_router_delete_scope_uses_events_not_stale_projection(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        store = _store_with_stale_projection(td, monkeypatch)

        removed = store.delete_scope("session", session_id="session-1")
        raw_events = store.events_path.read_text(encoding="utf-8")
        records = store.export_state()["router"]["records"]

    assert removed == 1
    assert "session-2" in raw_events
    assert [record["session_id"] for record in records] == ["session-2"]


def test_router_append_blocks_when_event_log_is_unreadable() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = GhostRouteStore(td)
        router = GhostRouter(store)
        router.route(
            _request(session_id="session-old", task="old"),
            provider_factory=lambda _provider_id: _Provider('{"mode":"research","confidence":0.9,"reason":"fresh"}'),
        )
        before = store.export_state()["router"]["records"]
        store.events_path.write_bytes(b"\xff\xfe\xff")
        request = _request(session_id="session-new", task="new")
        ok = store.append_result(
            finalize_route_decision(
                request,
                GhostRouteDecision("research", 0.9, "fresh", True),
            ),
            request,
        )
        after = store.export_state()["router"]["records"]

    assert not ok
    assert before == after
    assert store.last_warnings == ("router_events_unreadable",)


def test_router_compaction_does_not_empty_oversized_events_without_projection(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(router_module, "MAX_ROUTER_EVENTS_BYTES", 16)
        store = GhostRouteStore(td)
        store.directory.mkdir(parents=True, exist_ok=True)
        store.events_path.write_text("x" * 64, encoding="utf-8")
        before = store.events_path.read_bytes()

        result = store.compact_if_needed()
        after = store.events_path.read_bytes()

    assert result["ok"] is False
    assert result["compacted"] is False
    assert before == after
    assert "router_events_too_large" in result["warnings"]