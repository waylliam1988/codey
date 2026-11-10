from __future__ import annotations

import pytest

from codey.codemode.catalog import build_codemode_catalog, render_declarations
from codey.codemode.replay import repair_catalog_digest
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("kind", ["chat", "research", "hybrid"])
def test_restored_browser_contract_preserves_mutations_evidence_and_repair_identity(kind: str, native: bool) -> None:
    session = TaskSession(task_kind=kind, policy=TaskPolicy(grants=frozenset({
        "control", "web.read", "project.read", "project.write", "browser.observe", "browser.interact", "browser.effect",
    }), strict_research=kind == "research"))
    original = build_turn_snapshot(session, native=native)
    restored = restore_snapshot(snapshot_description(original))
    assert restored == original
    catalogs = [build_codemode_catalog(
        [spec for spec in snapshot.frozen_specs if spec.name in snapshot.nested_tool_names],
        allow_writes=True, allow_project_run="run" in snapshot.nested_tool_names,
    ) for snapshot in (original, restored)]
    assert catalogs[0].digest == catalogs[1].digest
    assert repair_catalog_digest(catalogs[0], ["browser_click"]) == repair_catalog_digest(catalogs[1], ["browser_click"])
    specs = {spec.name: spec for spec in restored.frozen_specs}
    assert not specs["browser_click"].read_only
    assert not specs["browser_fill"].read_only
    assert not specs["browser_navigate"].read_only
    assert specs["browser_read"].evidence_route == "source_read"
    assert specs["codemode"].declarations == render_declarations(catalogs[1])
