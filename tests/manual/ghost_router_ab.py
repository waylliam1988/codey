"""Manual A/B probe for a future automatic task router.

The probe simulates how an automatic router would choose between Chat,
read-only planning, Research, Project Writer, Hybrid, and Review. It does not
change production routing, write project files, run tools, or call the Codey
task runner.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.providers import controls as provider_controls
from codey.ghost.router import (
    GhostRouteDecision as RouterDecision,
    GhostRouteRequest,
    ROUTER_MODES,
    normalize_route_mode,
    parse_route_reply,
    render_route_prompt as render_production_route_prompt,
    route_error_cost,
)
from codey.ghost.schema import clip_signal_text
from codey.providers.registry import PROVIDER_TYPES, connect_provider, provider_ids


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = ROOT / "tests" / "fixtures" / "ghost_router_cases.jsonl"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
ARMS = ("baseline", "router")
MODES = ROUTER_MODES
parse_router_reply = parse_route_reply


@dataclass(frozen=True)
class RouterCase:
    name: str
    project: bool
    has_reviewable_diff: bool
    task: str
    expected_mode: str
    risk: str
    notes: str = ""


class FakeProvider:
    name = "fake"
    location = "fake://ghost-router"

    def new_chat(self, timeout: float | None = None) -> None:
        return None

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout
        task = _prompt_task(text).casefold()
        project = "Project attached: yes" in text
        if "review" in task and ("diff" in task or "findings" in task):
            return _reply("review", "review-only diff request")
        if any(term in task for term in ("查一下", "调研", "最新", "today", "latest")):
            if project and any(term in task for term in ("更新", "改", "write", "edit")) and "不要动" not in task:
                return _reply("hybrid", "research first, then project write")
            return _reply("research", "fresh external information requested")
        if any(term in task for term in ("先别改", "不要改", "方案", "plan", "readonly", "read-only")):
            return _reply("planning_readonly", "read-only planning requested")
        if project and any(term in task for term in ("修", "改", "更新", "readme", "pytest", "edit", "write")):
            return _reply("project_writer", "local project modification requested")
        return _reply("chat", "ordinary conversation")

    def close(self) -> None:
        return None


def load_cases(path: Path = DEFAULT_CASES) -> tuple[RouterCase, ...]:
    rows: list[RouterCase] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSONL row: {exc}") from exc
        case = RouterCase(
            name=str(payload.get("name") or "").strip(),
            project=bool(payload.get("project")),
            has_reviewable_diff=bool(payload.get("has_reviewable_diff")),
            task=str(payload.get("task") or "").strip(),
            expected_mode=normalize_route_mode(str(payload.get("expected_mode") or "")),
            risk=str(payload.get("risk") or "").strip() or "unknown",
            notes=str(payload.get("notes") or "").strip(),
        )
        if not case.name or not case.task or case.expected_mode not in MODES:
            raise ValueError(f"{path}:{line_number}: invalid router case")
        rows.append(case)
    return tuple(rows)


def run_cases(
    provider,
    *,
    provider_id: str,
    cases: tuple[RouterCase, ...],
    timeout: float,
    new_chat_timeout: float,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        rows.append(_score_case(
            case,
            arm="baseline",
            provider_id=provider_id,
            decision=baseline_decision(case),
            elapsed=0.0,
            prompt="",
            reply="",
            error="",
        ))
        prompt = render_router_prompt(case)
        started = time.time()
        try:
            provider.new_chat(timeout=new_chat_timeout)
            reply = provider.send(prompt, timeout=timeout)
            error = ""
        except Exception as exc:
            reply = ""
            error = f"{type(exc).__name__}: {clip_signal_text(exc, 240)}"
        decision = parse_router_reply(reply)
        rows.append(_score_case(
            case,
            arm="router",
            provider_id=provider_id,
            decision=decision,
            elapsed=round(time.time() - started, 3),
            prompt=prompt,
            reply=reply,
            error=error,
        ))
    summary = summarize_rows(rows)
    return {
        "provider": provider_id,
        "arms": list(ARMS),
        "cases": [case.name for case in cases],
        "ok": bool(summary["router"]["exact"] == summary["router"]["total"])
        and summary["delta"]["cost"] >= 0
        and summary["delta"]["severe_errors"] >= 0,
        "summary": summary,
        "rows": rows,
    }


def baseline_decision(case: RouterCase) -> RouterDecision:
    # This mirrors the current coarse auto-routing behavior: a local project
    # tends to become a writer task; no project tends to become chat.
    mode = "project" if case.project else "chat"
    return RouterDecision(
        mode=mode,
        confidence=1.0,
        reason="current auto fallback: project implies writer, no project implies chat",
        parse_ok=True,
    )


def render_router_prompt(case: RouterCase) -> str:
    return render_production_route_prompt(
        GhostRouteRequest(
            task=case.task,
            baseline_mode="project" if case.project else "chat",
            project="E:/project" if case.project else "",
            provider_id="manual",
            has_reviewable_diff=case.has_reviewable_diff,
        )
    )


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for arm in ARMS:
        arm_rows = [row for row in rows if row.get("arm") == arm]
        summary[arm] = {
            "total": len(arm_rows),
            "exact": sum(1 for row in arm_rows if row.get("exact")),
            "cost": sum(int(row.get("error_cost") or 0) for row in arm_rows),
            "severe_errors": sum(1 for row in arm_rows if row.get("severe_error")),
            "parse_errors": sum(1 for row in arm_rows if not row.get("parse_ok")),
        }
    summary["delta"] = {
        "exact": summary["router"]["exact"] - summary["baseline"]["exact"],
        "cost": summary["baseline"]["cost"] - summary["router"]["cost"],
        "severe_errors": summary["baseline"]["severe_errors"] - summary["router"]["severe_errors"],
    }
    return summary


def _score_case(
    case: RouterCase,
    *,
    arm: str,
    provider_id: str,
    decision: RouterDecision,
    elapsed: float,
    prompt: str,
    reply: str,
    error: str,
) -> dict[str, Any]:
    exact = decision.parse_ok and not error and decision.mode == case.expected_mode
    error_cost = 0 if exact else route_error_cost(case.expected_mode, decision.mode)
    return {
        "provider": provider_id,
        "case": case.name,
        "arm": arm,
        "project": case.project,
        "risk": case.risk,
        "expected_mode": case.expected_mode,
        "selected_mode": decision.mode,
        "exact": exact,
        "error_cost": error_cost,
        "severe_error": error_cost >= 5,
        "confidence": decision.confidence,
        "parse_ok": decision.parse_ok,
        "diagnostics": list(decision.diagnostics),
        "reason": decision.reason,
        "elapsed": elapsed,
        "reply": clip_signal_text(reply, 800),
        "error": error,
        "prompt_chars": len(prompt),
        "prompt_exposes_internal_names": _prompt_exposes_internal_names(prompt),
    }


def _prompt_task(prompt: str) -> str:
    marker = "\nUser request excerpt:\n"
    if marker not in prompt:
        return prompt
    return prompt.split(marker, 1)[1].strip()


def _reply(mode: str, reason: str) -> str:
    return json.dumps({"mode": mode, "confidence": 0.92, "reason": reason}, ensure_ascii=False)


def _prompt_exposes_internal_names(prompt: str) -> bool:
    text = str(prompt or "").casefold()
    return "codey" in text or "ghost" in text


def _selected_cases(cases: tuple[RouterCase, ...], names: list[str]) -> tuple[RouterCase, ...]:
    if not names:
        return cases
    wanted = set(names)
    selected = tuple(case for case in cases if case.name in wanted)
    missing = sorted(wanted - {case.name for case in selected})
    if missing:
        raise SystemExit(f"unknown case(s): {', '.join(missing)}")
    return selected


def _connect_live_provider(
    provider_id: str,
    *,
    port: int,
    open_if_missing: bool,
    isolated: bool,
):
    if not isolated:
        return connect_provider(
            provider_id,
            port=port,
            open_if_missing=open_if_missing,
        )
    provider_type = PROVIDER_TYPES.get(provider_id)
    if provider_type is None:
        raise ValueError(f"unsupported provider: {provider_id}")
    if provider_id == "local":
        return provider_type.connect()
    return provider_type.connect(
        port=port,
        open_if_missing=open_if_missing,
        bring_to_front=True,
        isolated=True,
    )


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _self_test() -> None:
    cases = load_cases()
    payload = run_cases(
        FakeProvider(),
        provider_id="fake",
        cases=cases,
        timeout=1,
        new_chat_timeout=1,
    )
    if not payload["ok"]:
        raise AssertionError(payload)
    if payload["summary"]["baseline"]["cost"] <= payload["summary"]["router"]["cost"]:
        raise AssertionError("router should improve over current auto baseline")
    if any(row["prompt_exposes_internal_names"] for row in payload["rows"]):
        raise AssertionError("router prompts must not expose internal names")
    print("self-test ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one-provider Ghost Router A/B simulation")
    parser.add_argument("--provider", choices=provider_ids(), default="deepseek")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--new-chat-timeout", type=float, default=45.0)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--no-open-if-missing", action="store_true")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--isolated", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        _self_test()
        return 0

    provider_id = str(args.provider).strip().lower()
    output = args.output or RESULTS_DIR / f"ghost_router_{provider_id}.json"
    cases = _selected_cases(load_cases(args.cases), args.case)
    provider_controls.begin_task_context(f"ghost-router-ab:{provider_id}")
    provider = None
    try:
        provider = _connect_live_provider(
            provider_id,
            port=args.port,
            open_if_missing=not args.no_open_if_missing,
            isolated=args.isolated,
        )
        payload = run_cases(
            provider,
            provider_id=provider_id,
            cases=cases,
            timeout=args.timeout,
            new_chat_timeout=args.new_chat_timeout,
        )
    except Exception as exc:
        payload = {
            "provider": provider_id,
            "ok": False,
            "error": f"{type(exc).__name__}: {clip_signal_text(exc, 240)}",
            "rows": [{
                "provider": provider_id,
                "case": "connect_or_run",
                "arm": "router",
                "exact": False,
                "error": f"{type(exc).__name__}: {clip_signal_text(exc, 240)}",
            }],
        }
    finally:
        provider_controls.end_task_context()
        if provider is not None and not args.keep_open:
            try:
                provider.close()
            except Exception:
                pass
    _write_report(output, payload)
    print(json.dumps({"ok": bool(payload.get("ok")), "output": str(output)}, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
