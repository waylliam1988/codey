"""A/B 分组必须真实收窄工具范围，而不只是改 prompt 文案。

同一快照进入提示、schema、解析与执行：baseline 打开来源后也不得
见到 source_search；source_search 组在打开来源后可见。捕获来源打开
后的各 arm 工具契约，而不是只检查 codec 文本。
"""

from __future__ import annotations

from codey.providers.base import tools_from_specs


def _strict_policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(
        grants=frozenset({"control", "web.read", "knowledge.read", "knowledge.write", "knowledge.link"}),
        strict_research=True,
    )


def _opened_session(*, denied=()):
    from codey.operations.task_session import TaskSession

    session = TaskSession(
        policy=_strict_policy(), task_kind="research", max_turns=4,
        task_text="q", controller_denied=tuple(denied),
    )
    session.search_results["r1"] = "https://example.com/a"
    session.opened_sources.add("https://example.com/a")
    return session


def test_baseline_arm_hides_source_search_after_open():
    from codey.operations.kernel_protocol import build_turn_snapshot

    snapshot = build_turn_snapshot(_opened_session(denied=("source_search",)), native=True)
    assert "open_url" in snapshot.tool_names
    assert "knowledge_write" in snapshot.tool_names
    assert "source_search" not in snapshot.tool_names
    schema_names = {item.name for item in tools_from_specs(snapshot.frozen_specs)}
    assert "source_search" not in schema_names
    assert "source_search" not in snapshot.contract_text


def test_source_search_arm_shows_source_search_after_open():
    from codey.operations.kernel_protocol import build_turn_snapshot

    snapshot = build_turn_snapshot(_opened_session(), native=True)
    assert "source_search" in snapshot.tool_names
    schema_names = {item.name for item in tools_from_specs(snapshot.frozen_specs)}
    assert "source_search" in schema_names


def test_baseline_arm_rejects_forced_source_search_call():
    from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn

    snapshot = build_turn_snapshot(_opened_session(denied=("source_search",)))
    plan = normalize_turn(
        '{"tool": "source_search", "args": {"url": "https://example.com/a", "query": "q"}}',
        snapshot=snapshot,
    )
    assert getattr(plan, "protocol_error", "") != ""
    assert plan.calls == []


def test_probe_baseline_arm_denies_source_search():
    from tests.manual import deep_research_core_ab as ab

    assert ab._arm_controller_denied("baseline") == ("source_search",)
    assert ab._arm_controller_denied("source_search") == ()


def test_denied_scope_survives_explicit_state_copy():
    session = _opened_session(denied=("source_search",))
    clone = _opened_session(denied=tuple(session.controller_denied))
    assert tuple(clone.controller_denied) == ("source_search",)
