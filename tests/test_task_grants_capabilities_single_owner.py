"""Task grant vocabulary has one leaf owner (no import fallback).

``codey.policies.capabilities`` owns ``KNOWN_TASK_GRANTS``. Both the task
policy and the tool registry consume it. The registry never falls back to a
hardcoded grant set when the policy module fails to import: configuration
errors must surface instead of silently changing authorization.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_capabilities_leaf_owns_grant_vocabulary() -> None:
    leaf = ROOT / "codey/policies/capabilities.py"
    assert leaf.exists(), "missing codey/policies/capabilities.py"
    from codey.policies.capabilities import KNOWN_TASK_GRANTS

    assert "control" in KNOWN_TASK_GRANTS
    assert "project.write" in KNOWN_TASK_GRANTS


def test_policy_and_registry_share_leaf_owner() -> None:
    from codey.policies import task_policy
    from codey.policies.capabilities import KNOWN_TASK_GRANTS as leaf

    assert task_policy.KNOWN_TASK_GRANTS is leaf
    policy_text = (ROOT / "codey/policies/task_policy.py").read_text(encoding="utf-8-sig")
    spec_text = (ROOT / "codey/toolchain/tool_spec.py").read_text(encoding="utf-8-sig")
    assert "codey.policies.capabilities" in policy_text
    assert "codey.policies.capabilities" in spec_text
    assert "KNOWN_TASK_GRANTS = frozenset" not in policy_text
    assert 'frozenset({"control"})' not in spec_text


def test_unknown_grant_registration_stays_rejected() -> None:
    from codey.toolchain.tool_spec import register_custom_tool, unregister_custom_tool

    assert register_custom_tool("probe_xyz_grant", grant="nope") is False
    unregister_custom_tool("probe_xyz_grant")
