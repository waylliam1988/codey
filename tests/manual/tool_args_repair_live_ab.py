"""Live provider A/B probe for Tool Argument Repair.

Runs the production coding agent loop against a real provider on tiny temporary
projects. The baseline arm uses a 0.5.2-shaped strict codec; the candidate arm
uses the current 0.5.3 JsonToolCodec.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.agents import runner as agent
from codey.agents.request import AgentRequest
from codey.providers import controls as provider_controls
from codey.providers.registry import DEFAULT_PROVIDER_ID, connect_provider, provider_ids
from codey.protocols.json_codec import (
    PROTOCOL_INVALID_ARGS,
    JsonToolCodec,
    _balanced_json_objects,
)
from codey.runtime.events import render_run_event
from codey.runtime.models import ToolPlan
from codey.runs.trace import RunTraceStore


RESULTS_DIR = Path(__file__).resolve().parent / "results"
ARMS = ("baseline", "candidate")


@dataclass(frozen=True)
class Case:
    name: str
    task: str
    files: dict[str, str]
    expected_final: dict[str, str]


CASES = (
    Case(
        name="search_and_read",
        task=(
            "Find where TARGET_TOKEN is defined, read the file, and finish with "
            "the file path in the summary. Use the local JSON tools."
        ),
        files={
            "src/auth/service.py": "TARGET_TOKEN = 'alpha'\n",
            "README.md": "# Fixture\n",
        },
        expected_final={
            "src/auth/service.py": "TARGET_TOKEN = 'alpha'\n",
            "README.md": "# Fixture\n",
        },
    ),
    Case(
        name="small_edit_and_verify",
        task=(
            "Change only FEATURE_FLAG from False to True in config.py, then run "
            "python -m py_compile config.py before finishing."
        ),
        files={"config.py": "FEATURE_FLAG = False\n"},
        expected_final={"config.py": "FEATURE_FLAG = True\n"},
    ),
)


def _object_args(obj: dict[str, Any]) -> dict[str, Any]:
    if "args" in obj:
        args = obj.get("args")
        return args if isinstance(args, dict) else {}
    return {key: value for key, value in obj.items() if key not in {"tool", "name"}}


def _baseline_friction(obj: dict[str, Any]) -> tuple[str, str]:
    tool = str(obj.get("tool") or obj.get("name") or "").lower().strip()
    args = _object_args(obj)

    if "cwd" in args and "path" not in args:
        return "path requires path, not cwd", tool
    if tool in {"grep", "search"} and "pattern" in args and "query" not in args:
        return "grep requires a query", "search"
    if tool in {"read_file", "read"}:
        if isinstance(args.get("offset"), str) or isinstance(args.get("limit"), str):
            return "offset/limit must be integers", "read"
    if tool in {"find_references", "references"} and "name" in args and "symbol" not in args:
        return "find_references requires a symbol", "references"
    if tool in {"run", "shell"} and "cmd" in args and "command" not in args:
        return f"{tool} requires a command", tool
    if tool == "edit":
        old_aliases = {"search", "old", "before"}
        new_aliases = {"replace", "replacement", "after", "new"}
        if any(key in args for key in old_aliases | new_aliases):
            return "edit requires old_string and new_string", "edit"
        replacements = args.get("replacements")
        if isinstance(replacements, str):
            return "edit replacements must be a list", "edit"
        if isinstance(replacements, dict):
            return "edit replacements must be a list", "edit"
        if isinstance(replacements, list):
            for item in replacements:
                if isinstance(item, dict) and any(key in item for key in old_aliases | new_aliases):
                    return "edit replacement items require old_string and new_string", "edit"

    return "", ""


class Baseline052Codec(JsonToolCodec):
    name = "json_baseline_052"

    def parse(self, text: str) -> ToolPlan:
        for obj in _balanced_json_objects(text):
            message, tool_name = _baseline_friction(obj)
            if message:
                return ToolPlan(
                    calls=[],
                    control=None,
                    protocol_error=message,
                    protocol_error_kind=PROTOCOL_INVALID_ARGS,
                    protocol_tool_name=tool_name,
                )
        return super().parse(text)


def _write_case(root: Path, case: Case) -> None:
    for rel, content in case.files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _changed_files(root: Path, original: dict[str, str]) -> tuple[str, ...]:
    changed: list[str] = []
    for rel, before in original.items():
        path = root / rel
        if not path.is_file() or path.read_text(encoding="utf-8") != before:
            changed.append(rel)
    return tuple(sorted(changed))


def _expected_content_ok(root: Path, expected: dict[str, str]) -> bool:
    for rel, content in expected.items():
        path = root / rel
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            return False
    return True


def _protocol_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    phases = payload.get("protocol_telemetry", {}).get("phases", {})
    writer = phases.get("writer", {}) if isinstance(phases, dict) else {}
    return {
        "protocol_error_count": sum(
            int(value or 0)
            for value in dict(writer.get("protocol_error_counts") or {}).values()
        ),
        "repair_prompt_count": int(writer.get("repair_prompt_count") or 0),
        "first_valid_turn": int(writer.get("first_valid_turn") or 0),
        "valid_turns": list(writer.get("valid_turns") or []),
        "alias_rewrite_count": int(writer.get("alias_rewrite_count") or 0),
        "arg_repair_counts": dict(writer.get("arg_repair_counts") or {}),
    }


def _run_agent_case(
    provider: Any,
    *,
    provider_id: str,
    case: Case,
    arm: str,
    max_turns: int,
    fresh_chat: bool,
    probe_slug: str = "tool-args-repair-live-ab",
) -> dict[str, Any]:
    started = time.time()
    with tempfile.TemporaryDirectory(prefix=f"codey-tool-args-{case.name}-{arm}-") as td:
        root = Path(td) / "project"
        root.mkdir()
        _write_case(root, case)
        trace_store = RunTraceStore(Path(td) / "state")
        run_id = f"{probe_slug}-{case.name}-{arm}"
        session_id = f"{probe_slug}-{provider_id}-{case.name}-{arm}"
        trace = trace_store.open(
            run_id=run_id,
            session_id=session_id,
            project=root,
            mode_initial="project",
            provider_initial=provider_id,
        )
        events: list[str] = []
        codec = Baseline052Codec() if arm == "baseline" else JsonToolCodec()
        try:
            result = agent.run(AgentRequest(
                provider=provider,
                project=root,
                task=case.task,
                codec=codec,
                max_turns=max_turns,
                fresh_chat=fresh_chat,
                provider_id=provider_id,
                on_event=lambda event: events.append(render_run_event(event)),
                trace_recorder=trace,
                session_id=session_id,
                run_id=run_id,
            ))
            trace.finish(status=result.stop_reason, mode="project", provider=provider_id)
            payload = json.loads(
                trace_store.path_for(session_id, run_id).read_text(encoding="utf-8")
            )
            return {
                "case": case.name,
                "arm": arm,
                "ok": True,
                "seconds": round(time.time() - started, 3),
                "stop_reason": result.stop_reason,
                "turns": result.turns,
                "summary_chars": len(result.summary),
                "changed_files": _changed_files(root, case.files),
                "expected_content_ok": _expected_content_ok(root, case.expected_final),
                "protocol": _protocol_metrics(payload),
                "event_count": len(events),
            }
        except Exception as exc:
            trace.finish(status="error", mode="project", provider=provider_id)
            return {
                "case": case.name,
                "arm": arm,
                "ok": False,
                "seconds": round(time.time() - started, 3),
                "error": f"{type(exc).__name__}: {exc}",
                "event_count": len(events),
            }


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"arms": {}}
    for arm in ARMS:
        arm_rows = [row for row in rows if row.get("arm") == arm and row.get("ok")]
        if not arm_rows:
            summary["arms"][arm] = {"count": 0}
            continue
        summary["arms"][arm] = {
            "count": len(arm_rows),
            "done": sum(1 for row in arm_rows if row.get("stop_reason") == "done"),
            "expected_content_ok": sum(1 for row in arm_rows if row.get("expected_content_ok")),
            "turns": sum(int(row.get("turns") or 0) for row in arm_rows),
            "protocol_error_count": sum(
                int(row.get("protocol", {}).get("protocol_error_count") or 0)
                for row in arm_rows
            ),
            "repair_prompt_count": sum(
                int(row.get("protocol", {}).get("repair_prompt_count") or 0)
                for row in arm_rows
            ),
            "alias_rewrite_count": sum(
                int(row.get("protocol", {}).get("alias_rewrite_count") or 0)
                for row in arm_rows
            ),
        }
    baseline = summary["arms"].get("baseline", {})
    candidate = summary["arms"].get("candidate", {})
    if baseline.get("count") and candidate.get("count"):
        summary["candidate_delta_vs_baseline"] = {
            "turns": int(candidate.get("turns") or 0) - int(baseline.get("turns") or 0),
            "protocol_error_count": (
                int(candidate.get("protocol_error_count") or 0)
                - int(baseline.get("protocol_error_count") or 0)
            ),
            "repair_prompt_count": (
                int(candidate.get("repair_prompt_count") or 0)
                - int(baseline.get("repair_prompt_count") or 0)
            ),
        }
    return summary


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _default_output(provider_id: str) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return RESULTS_DIR / f"tool_args_repair_live_ab-{provider_id}-{stamp}.json"


def _select_arms(values: list[str]) -> tuple[str, ...]:
    raw_values = values or ["baseline,candidate"]
    selected: list[str] = []
    for value in raw_values:
        for item in str(value or "").split(","):
            arm = item.strip().lower()
            if arm and arm not in selected:
                selected.append(arm)
    unknown = [arm for arm in selected if arm not in ARMS]
    if unknown:
        raise SystemExit(f"unknown arm(s): {', '.join(unknown)}")
    return tuple(selected or ARMS)


def _select_cases(values: list[str]) -> tuple[Case, ...]:
    index = {case.name: case for case in CASES}
    if not values:
        return CASES
    selected: list[str] = []
    for value in values:
        for item in str(value or "").split(","):
            name = item.strip()
            if name and name not in selected:
                selected.append(name)
    unknown = [name for name in selected if name not in index]
    if unknown:
        raise SystemExit(f"unknown case(s): {', '.join(unknown)}")
    return tuple(index[name] for name in selected)


def _self_test() -> None:
    baseline = Baseline052Codec()
    candidate = JsonToolCodec()
    samples = (
        '{"tool":"grep","args":{"pattern":"needle","path":"."}}',
        '{"tool":"edit","args":{"path":"a.py","old":"x","new":"y"}}',
        '{"tool":"read_file","args":{"path":"a.py","offset":"1"}}',
        '{"tool":"find_references","args":{"name":"Thing","path":"."}}',
        '{"tool":"run","args":{"cmd":"python -m unittest","path":"."}}',
    )
    for sample in samples:
        base = baseline.parse(sample)
        current = candidate.parse(sample)
        assert base.protocol_error_kind == PROTOCOL_INVALID_ARGS, sample
        assert current.protocol_error == "", (sample, current.protocol_error)
    print("self-test ok")


def run_live(
    *,
    provider_id: str,
    port: int,
    max_turns: int,
    arms: tuple[str, ...],
    cases: tuple[Case, ...],
    fresh_chat: bool,
    keep_open: bool,
    output: Path,
) -> int:
    payload: dict[str, Any] = {
        "probe": "tool_args_repair_live_ab",
        "provider": provider_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "arms": list(arms),
        "cases": [case.name for case in cases],
        "rows": [],
        "summary": {},
    }
    _atomic_write_json(output, payload)
    provider_controls.begin_task_context(f"tool-args-repair-live-ab:{provider_id}")
    provider = None
    try:
        provider = connect_provider(provider_id, port=port)
        for case in cases:
            for arm in arms:
                row = _run_agent_case(
                    provider,
                    provider_id=provider_id,
                    case=case,
                    arm=arm,
                    max_turns=max_turns,
                    fresh_chat=fresh_chat,
                )
                payload["rows"].append(row)
                payload["summary"] = _summarize(payload["rows"])
                _atomic_write_json(output, payload)
                print(json.dumps(row, ensure_ascii=False), flush=True)
        payload["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        payload["summary"] = _summarize(payload["rows"])
        _atomic_write_json(output, payload)
        print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
        print(f"report: {output}")
        return 0 if all(row.get("ok") for row in payload["rows"]) else 1
    finally:
        if provider is not None and not keep_open:
            close = getattr(provider, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        provider_controls.end_task_context()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live provider A/B for 0.5.3 tool argument repair.")
    parser.add_argument("--provider", choices=provider_ids(), default=DEFAULT_PROVIDER_ID)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--arm", action="append", default=[], help="arm or comma list")
    parser.add_argument("--case", action="append", default=[], help="case or comma list")
    parser.add_argument("--reuse-chat", action="store_true")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        _self_test()
        return 0

    output = args.output or _default_output(args.provider)
    return run_live(
        provider_id=args.provider,
        port=args.port,
        max_turns=args.max_turns,
        arms=_select_arms(args.arm),
        cases=_select_cases(args.case),
        fresh_chat=not args.reuse_chat,
        keep_open=args.keep_open,
        output=output,
    )


if __name__ == "__main__":
    raise SystemExit(main())
