"""Research contracts expose their initialization dependencies without changing wire values."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def contract_surface() -> dict:
    from dataclasses import asdict

    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.providers.base import tools_from_specs
    from codey.research.tool_contract import TOOL_CONTRACTS
    from codey.toolchain.tool_spec import thaw_schema_value

    profiles = {}
    for purpose in ("ordinary", "evidence", "report"):
        session = TaskSession(TaskPolicy(
            frozenset({"control", "web.read", "knowledge.read", "knowledge.write", "knowledge.link"}),
            strict_research=purpose == "report"),
            task_kind="evidence_followup" if purpose == "evidence" else "research")
        session.research_questions = ("Main question",)
        for native in (False, True):
            snapshot = build_turn_snapshot(session, native=native)
            profiles[f"{purpose}:{native}"] = {
                "text": snapshot.contract_text,
                "tool_names": snapshot.tool_names,
                "native": [{"name": tool.name, "description": tool.description,
                            "parameters": thaw_schema_value(tool.parameters)}
                           for tool in tools_from_specs(snapshot.model_specs)],
            }
    return {"research": {name: asdict(contract) for name, contract in TOOL_CONTRACTS.items()}, "profiles": profiles}


def surface_digest() -> str:
    payload = json.dumps(contract_surface(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("first", [
    "codey.research.knowledge_schema", "codey.research.tool_contract", "codey.research.questions",
    "codey.research.answer_contract", "codey.toolchain.tool_spec",
])
def test_cold_imports_have_explicit_dependencies_and_preserve_contracts(first: str) -> None:
    script = (
        "import json, sys\n"
        "from tools.kernel_import_audit import cold_import\n"
        "from tests.test_research_contract_imports import surface_digest\n"
        "probe = cold_import(sys.argv[1])\n"
        "print(json.dumps({'probe': probe, 'surface_digest': surface_digest()}))\n"
    )
    child = subprocess.run(
        [sys.executable, "-B", "-c", script, first], cwd=ROOT,
        capture_output=True, text=True, check=True, timeout=20,
    )
    result = json.loads(child.stdout)
    assert result["probe"]["error"] is None, result["probe"]["error"]
    implicit = [event for event in result["probe"]["initialization_time_function_imports"]
                if event["source"] in {"codey.research.knowledge_schema", "codey.research.tool_contract"}]
    assert not implicit, implicit
    assert result["surface_digest"] == surface_digest(), first
