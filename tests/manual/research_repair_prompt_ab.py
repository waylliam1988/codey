"""Live A/B probe for Research protocol-repair prompts.

This is intentionally manual: it sends two protocol-repair prompts to a live
web provider and atomically writes one JSON result after each arm. It does not
run local Research tools, save notes, or change the production vault.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.providers import controls as provider_controls
from codey.research.controller import (
    ResearchControlState,
    ResearchController,
    render_control_block,
)
from codey.research.protocols import JsonToolCodec, extract_json_objects
from codey.research.runner import render_research_repair_prompt
from codey.research.tool_contract import PROTOCOL_TOO_MANY_TOOLS
from codey.providers.registry import connect_fresh_provider_tab, connect_provider, provider_ids

RESULTS_DIR = Path(__file__).resolve().parent / "results"


@dataclass(frozen=True)
class Arm:
    name: str
    prompt: str


def _first_turn_state() -> ResearchControlState:
    return ResearchControlState(
        allowed_tools=("knowledge_search", "knowledge_read", "web_search"),
        evidence_count=0,
        note_count=0,
    )


def _too_many_plan() -> SimpleNamespace:
    return SimpleNamespace(
        calls=[],
        control=None,
        protocol_error="too many JSON tool calls in one reply (2); reply with exactly one JSON object",
        protocol_error_kind=PROTOCOL_TOO_MANY_TOOLS,
    )


def _old_prompt() -> str:
    control = """Research controller current allowed actions:
- Allowed tools this turn: knowledge_search, knowledge_read, web_search
- Reply with exactly one JSON object using only the allowed tools below.
- Prefer result_id/source_id/hit_id over hand-copying URLs when an ID is available.
- Saved evidence items: 0; saved/updated notes: 0.
- In done.answer, cite and list only evidence-backed source URLs. Opened-only sources are not citable yet.

Allowed JSON shapes:
- {"tool":"knowledge_search","args":{"query":"..."}}
- {"tool":"knowledge_read","args":{"id":"<note id>"}}
- {"tool":"web_search","args":{"query":"..."}}

Do not call tools outside the allowed list. Do not output multiple JSON objects."""
    repair = """Your last reply did not satisfy Codey's Research tool contract.
Error: too many JSON tool calls in one reply (2); reply with exactly one JSON object

Codey Research executes exactly one action per turn.
Choose one next action and reply with only that JSON object.

Example:
{"tool":"knowledge_search","args":{"query":"..."}}

Reply with exactly one JSON object and no prose."""
    return repair + "\n\n" + control


def _new_prompt() -> str:
    state = _first_turn_state()
    repair = render_research_repair_prompt(JsonToolCodec(), _too_many_plan(), state)
    return repair.rstrip() + "\n\n" + render_control_block(state)


def _arms(order: str) -> list[Arm]:
    mapping = {
        "old": Arm("old", _old_prompt()),
        "new": Arm("new", _new_prompt()),
    }
    names = [item.strip().lower() for item in order.split(",") if item.strip()]
    if not names:
        names = ["old", "new"]
    unknown = [name for name in names if name not in mapping]
    if unknown:
        raise SystemExit(f"unknown arm(s): {', '.join(unknown)}")
    return [mapping[name] for name in names]


def _analyze_reply(reply: str) -> dict[str, Any]:
    state = _first_turn_state()
    objects = extract_json_objects(reply)
    tools = [
        str(obj.get("tool") or "").strip().lower()
        for obj in objects
    ]
    controller = ResearchController()
    plan = controller.parse_plan(JsonToolCodec(), reply, state)
    tool = ""
    args: dict[str, Any] = {}
    if plan.calls:
        tool = plan.calls[0].name
        args = dict(plan.calls[0].args)
    elif plan.control is not None:
        tool = plan.control.kind
    return {
        "json_object_count": len(objects),
        "tools": tools,
        "accepted": bool((plan.calls or plan.control is not None) and not plan.protocol_error),
        "accepted_tool": tool,
        "accepted_args": args,
        "protocol_error_kind": plan.protocol_error_kind,
        "protocol_error": plan.protocol_error,
        "reply_chars": len(reply),
        "reply_excerpt": reply[:1500],
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _result_path(provider: str) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return RESULTS_DIR / f"research_repair_prompt_ab-{provider}-{stamp}.json"


def _self_test() -> None:
    old = _old_prompt()
    new = _new_prompt()
    assert "Allowed JSON shapes:" in old
    assert "Allowed JSON shapes for this turn (choose exactly one):" in new
    assert "Tools not listed here are forbidden this turn" in new
    assert "Example:\n{\"tool\":\"knowledge_search\"" in old
    assert "Do not repeat the same JSON object twice." in new
    accepted = _analyze_reply('{"tool":"web_search","args":{"query":"铝合金 美伊战争"}}')
    assert accepted["accepted"] is True
    assert accepted["accepted_tool"] == "web_search"
    blocked = _analyze_reply('{"tool":"knowledge_write","args":{"title":"x","content":"y"}}')
    assert blocked["accepted"] is False
    assert blocked["protocol_error_kind"] == "disallowed_tool"


def main() -> int:
    choices = tuple(provider_id for provider_id in provider_ids() if provider_id != "local")
    parser = argparse.ArgumentParser(description="Live A/B probe for Research repair prompts")
    parser.add_argument("--provider", choices=choices, default="mimo")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--order", default="old,new", help="comma-separated arms: old,new")
    parser.add_argument("--fresh", action="store_true", help="open an isolated fresh provider tab")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        _self_test()
        print("self-test ok")
        return 0

    arms = _arms(args.order)
    result_path = _result_path(args.provider)
    result: dict[str, Any] = {
        "provider": args.provider,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "order": [arm.name for arm in arms],
        "result_path": str(result_path),
        "arms": [],
    }
    _atomic_write_json(result_path, result)

    provider_controls.begin_task_context(f"research-repair-prompt-ab:{args.provider}")
    provider = None
    try:
        provider = (
            connect_fresh_provider_tab(args.provider, port=args.port)
            if args.fresh
            else connect_provider(args.provider, port=args.port)
        )
        for arm in arms:
            started = time.time()
            row: dict[str, Any] = {
                "arm": arm.name,
                "prompt_chars": len(arm.prompt),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            try:
                provider.new_chat(timeout=args.timeout)
                reply = provider.send(arm.prompt, timeout=args.timeout)
                row.update({
                    "seconds": round(time.time() - started, 3),
                    "ok": True,
                    "analysis": _analyze_reply(reply),
                })
            except Exception as exc:
                row.update({
                    "seconds": round(time.time() - started, 3),
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            result["arms"].append(row)
            _atomic_write_json(result_path, result)
            print(json.dumps(row, ensure_ascii=False), flush=True)
        result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        _atomic_write_json(result_path, result)
        print(f"wrote {result_path}")
        return 0 if all(row.get("ok") for row in result["arms"]) else 1
    finally:
        if provider is not None and not args.keep_open:
            try:
                provider.close()
            except Exception:
                pass
        provider_controls.end_task_context()


if __name__ == "__main__":
    raise SystemExit(main())