"""New task defaults reach the frozen browser catalog without a UI opt-in."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.durable import DurableSession, SQLiteDurableStore
from codey.operations.recovery import policy_from_durable_task
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.capabilities import BROWSER_CAPABILITIES
from codey.policies.task_policy import TaskPolicy, build_task_policy
from codey.providers.token_accounting import ApiExchangeUsage, ContextBudget
from codey.task.entry_auth import derive_entry_auth
from codey.task.model import TaskSubmission
from codey.types.json_value import JsonObject


@pytest.mark.parametrize("kind", ["auto", "chat", "project", "hybrid", "research"])
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("native", [False, True])
def test_new_tasks_have_web_and_browser_tools_without_requested_grants(kind: str, strict: bool, native: bool) -> None:
    request = TaskSubmission("s", "/project" if kind in {"project", "hybrid"} else None,
                             "Inspect a website", 8, False, "local", strict_research=strict)
    policy = build_task_policy(request, task_kind=kind)
    assert policy.allows("web.read")
    assert all(policy.allows(grant) for grant in BROWSER_CAPABILITIES)
    assert not policy.sources_open_required
    session = TaskSession(policy=policy, task_kind=kind)
    snapshot = build_turn_snapshot(session, native=native)
    if strict:
        assert "submit_research_answer" in snapshot.inventory_names
        assert "browser_read" in snapshot.inventory_names
        assert "research_plan" not in snapshot.inventory_names
    assert {"web_search", "open_url", "browser_navigate", "browser_snapshot", "browser_fill",
            "browser_click", "browser_scroll", "browser_tab_switch"} <= set(snapshot.inventory_names)
    assert {"web_search", "open_url"} <= set(snapshot.tool_names)
    assert TaskPolicy.from_payload(policy.to_payload()) == policy


@pytest.mark.parametrize("kind", ["auto", "chat", "project", "hybrid", "research", "planning"])
def test_explicit_network_off_removes_default_web_and_browser_tools(kind: str) -> None:
    auth = derive_entry_auth({"intent": kind, "allow_web": False})
    request = TaskSubmission("s", None, "Local task", 8, False, "local",
                             denied_capabilities=auth.denied_capabilities)
    policy = build_task_policy(request, task_kind=kind)
    assert not policy.allows("web.read")
    assert not any(policy.allows(grant) for grant in BROWSER_CAPABILITIES)
    snapshot = build_turn_snapshot(TaskSession(policy=policy, task_kind=kind))
    assert "web_search" not in snapshot.inventory_names
    assert not any(name.startswith("browser_") for name in snapshot.inventory_names)


@pytest.mark.parametrize("kind", ["planning", "planning_readonly", "readonly"])
def test_readonly_modes_can_observe_but_cannot_interact(kind: str) -> None:
    request = TaskSubmission("s", None, "Read only", 8, False, "local")
    policy = build_task_policy(request, task_kind=kind)
    assert policy.allows("web.read") and policy.allows("browser.observe")
    assert not policy.allows("browser.interact")
    assert not policy.allows("browser.effect")


def test_recovery_keeps_the_original_policy_instead_of_adding_new_defaults() -> None:
    original = TaskPolicy(grants=frozenset({"control", "web.read"}))
    recovered = TaskPolicy.from_payload(original.to_payload())
    assert recovered == original
    assert not any(recovered.allows(grant) for grant in BROWSER_CAPABILITIES)


@pytest.mark.parametrize("kind", ["chat", "project", "hybrid", "research"])
def test_headless_entry_has_default_browser_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    captured: list[TaskSubmission] = []

    def capture(_deps: object, request: TaskSubmission) -> None:
        captured.append(request)

    monkeypatch.setattr("codey.app.headless_runner.run_task_submission", capture)
    project = tmp_path / "project" if kind in {"project", "hybrid"} else None
    run_headless(HeadlessRequest(project, "Inspect", intent=kind, state_home=tmp_path / "state"),
                 emit_jsonl=lambda _row: None)
    assert len(captured) == 1
    policy = build_task_policy(captured[0], task_kind=kind)
    assert all(policy.allows(grant) for grant in BROWSER_CAPABILITIES)
    assert policy.allows("web.read")


class ScriptedProvider:
    name = "Local"
    location = "fixture://local"
    context_budget = ContextBudget(32768, 8192, 0, 12000)

    def __init__(self, replies: tuple[JsonObject, ...]) -> None:
        self.replies = iter(replies)
        self.prompts: list[str] = []

    def new_chat(self, timeout: float | None = None) -> None:
        pass

    def bind_usage(self, connection_id: str, sink: Callable[[ApiExchangeUsage], None]) -> None:
        pass

    def send(self, text: str, timeout: float | None = None) -> str:
        self.prompts.append(text)
        return json.dumps(next(self.replies))

    def close(self) -> None:
        pass


@pytest.mark.usefixtures("scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("kind", ["auto", "project", "planning_readonly"])
def test_baseline_browser_catalog_keeps_real_project_reads_and_durable_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    monkeypatch.setenv("NATIVE_TOOLS", "0")
    project = tmp_path / "project"
    project.mkdir()
    text = "BASELINE_READ_MARKER = 42\n"
    target = project / "pricing.py"
    target.write_text(text, encoding="utf-8")
    provider = ScriptedProvider((
        {"tool": "codemode", "args": {
            "code": "return unwrap(await tools.read_file({path:inputs.path}));",
            "inputs": {"path": "pricing.py"}}},
        {"tool": "done", "args": {"summary": "BASELINE_READ_MARKER is 42."}},
    ))
    rows: list[dict[str, object]] = []

    def connect(provider_id: str, *, port: int) -> ScriptedProvider:
        return provider

    state = tmp_path / "state"
    result = run_headless(
        HeadlessRequest(project, "Read pricing.py and explain without editing files.",
                        provider_id="local", intent=kind, max_turns=3, state_home=state,
                        project_changes_required=False),
        emit_jsonl=rows.append, connect_provider=connect,
    )
    assert result.exit_code == 0 and result.stop_reason == "done", rows
    assert len(provider.prompts) == 2 and "BASELINE_READ_MARKER" in provider.prompts[-1]
    assert any(row.get("tool_name") == "read_file" and row.get("ok") is True for row in rows)
    assert target.read_text(encoding="utf-8") == text
    durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
    try:
        task = durable.task(result.run_id)
        assert task.phase == "completed"
        policy = policy_from_durable_task(durable, task_id=result.run_id)
        assert policy.allows("web.read") and policy.allows("browser.observe")
        assert policy.allows("browser.interact") is (kind != "planning_readonly")
        assert any(effect.settlement is not None for effect in durable.effects(result.run_id))
    finally:
        durable.close()


@pytest.mark.usefixtures("scripted_local_api_connection", "no_external_advisor_models", "public_source_dns")
@pytest.mark.parametrize("kind", ("chat", "project", "hybrid"))
def test_default_entry_searches_opens_and_delivers_observed_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    from codey.research.web_sources import WebSourceProvider

    monkeypatch.setenv("NATIVE_TOOLS", "0")
    url = "https://example.com/fuzhou"
    marker = "FUZHOU_OBSERVED_CITY_731"
    acquisitions: list[str] = []

    def search(self: WebSourceProvider, query: str, *, limit: int = 8) -> list[dict[str, object]]:
        acquisitions.append("search:" + query)
        return [{"url": url, "title": "Fuzhou", "snippet": "Open the city record."}]

    def fetch(self: WebSourceProvider, target: str) -> dict[str, object]:
        acquisitions.append("open:" + target)
        return {"status": "ok", "url": target, "title": "City record", "text": marker}

    monkeypatch.setattr(WebSourceProvider, "search", search)
    monkeypatch.setattr(WebSourceProvider, "fetch", fetch)
    provider = ScriptedProvider((
        {"tool": "web_search", "args": {"query": "Fuzhou city"}},
        {"tool": "open_result", "args": {"result_id": "r1"}},
        {"tool": "done", "args": {"summary": marker + " [s1]"}},
    ))
    rows: list[dict[str, object]] = []

    def connect(provider_id: str, *, port: int) -> ScriptedProvider:
        return provider

    project = None if kind == "chat" else tmp_path / "project"
    if project is not None:
        project.mkdir()
    result = run_headless(HeadlessRequest(project, "Search Fuzhou online and read the city record. No file changes.",
        provider_id="local", intent=kind, max_turns=4, state_home=tmp_path / "state", project_changes_required=False),
        emit_jsonl=rows.append, connect_provider=connect)
    assert result.exit_code == 0 and result.stop_reason == "done", rows
    assert acquisitions == ["search:Fuzhou city", "open:" + url]
    assert len(provider.prompts) == 3 and marker in provider.prompts[-1]
    assert {row.get("tool_name") for row in rows if row.get("ok") is True} >= {"web_search", "open_result"}
