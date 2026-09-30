"""AgentRequest carries only consumed inputs (no dead codec/delivery wiring).

Cold-start rule: an entry field without a production consumer is removed,
not kept for compatibility. ``codec`` no longer influences the unified
kernel protocol, and ``tool_result_delivery`` is owned by the runtime
delivery system, not threaded through ``AgentRequest``.
"""
from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_agent_request_has_no_codec_field() -> None:
    from codey.agents.request import AgentRequest

    names = {f.name for f in fields(AgentRequest)}
    assert "codec" not in names


def test_agent_request_has_no_tool_result_delivery_field() -> None:
    from codey.agents.request import AgentRequest

    names = {f.name for f in fields(AgentRequest)}
    assert "tool_result_delivery" not in names


def test_no_production_writer_passes_dead_request_fields() -> None:
    offenders: list[str] = []
    for path in (ROOT / "codey").rglob("*.py"):
        text = path.read_text(encoding="utf-8-sig")
        if "AgentRequest(" not in text:
            continue
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_request_call = (
                (isinstance(func, ast.Name) and func.id == "AgentRequest")
                or (isinstance(func, ast.Attribute) and func.attr == "AgentRequest")
            )
            if not is_request_call:
                continue
            for kw in node.keywords:
                if kw.arg in {"codec", "tool_result_delivery"}:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}:{kw.arg}")
    assert offenders == []


def test_no_production_code_reads_dead_request_fields() -> None:
    offenders: list[str] = []
    for path in (ROOT / "codey").rglob("*.py"):
        if path.name == "request.py" and path.parent.name == "agents":
            continue
        text = path.read_text(encoding="utf-8-sig")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "request.codec" in line or "request.tool_result_delivery" in line:
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert offenders == []
