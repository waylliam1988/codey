from __future__ import annotations

from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.toolchain.tool_spec import tool_specs


def test_browser_contracts_are_production_specs() -> None:
    specs = tool_specs()
    expected = {
        "browser_snapshot", "browser_read", "browser_click", "browser_fill",
        "browser_scroll", "browser_wait", "browser_navigate", "browser_eval",
        "browser_tab_list", "browser_tab_new", "browser_tab_switch",
        "browser_upload", "browser_download",
    }
    assert expected <= set(specs)
    assert all(spec.executor == "browser" for name, spec in specs.items() if name in expected)
    assert specs["browser_read"].evidence_route == "source_read"
    assert specs["browser_snapshot"].grant == "browser.observe"
    assert specs["browser_click"].grant == "browser.interact"
    assert specs["browser_upload"].grant == "browser.effect"


def test_browser_tools_are_hidden_without_browser_capability() -> None:
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "web.read"})), task_kind="research")
    snapshot = build_turn_snapshot(session)
    assert not any(name.startswith("browser_") for name in snapshot.tool_names)


def test_browser_surface_uses_the_same_frozen_snapshot() -> None:
    grants = frozenset({"control", "browser.observe", "browser.interact", "browser.effect"})
    session = TaskSession(policy=TaskPolicy(grants=grants), task_kind="chat")
    snapshot = build_turn_snapshot(session)
    names = set(snapshot.tool_names)
    assert {"browser_snapshot", "browser_read", "browser_click", "browser_fill"} <= names
    assert {spec.name for spec in snapshot.frozen_specs} == set(snapshot.inventory_names)
    assert snapshot.frozen_specs[snapshot.inventory_names.index("browser_read")].evidence_route == "source_read"
