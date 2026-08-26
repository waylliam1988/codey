"""Live A/B probe for coding protocol repair prompts.

This probe compares the legacy generic coding repair prompt against the
production typed repair prompt. It does not execute local tools or edit project
files. Each case starts from a deliberately invalid coding reply and asks a
live web provider to repair it into exactly one local-runner JSON command.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.agents.runner import _protocol_repair_prompt
from codey.providers import controls as provider_controls
from codey.runtime.models import ToolPlan
from codey.protocols import JsonToolCodec
from codey.protocols.json_codec import _balanced_json_objects
from codey.providers.registry import connect_fresh_provider_tab, connect_provider, provider_ids

RESULTS_DIR = Path(__file__).resolve().parent / "results"
ARMS = ("baseline", "typed")
MAX_REPLY_CHARS = 1_500


@dataclass(frozen=True)
class RepairCase:
    name: str
    bad_reply: str
    context: str
    valid_reply: dict[str, Any]
    expected: Callable[[ToolPlan], bool]


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _expected_call(name: str, **required_args: object) -> Callable[[ToolPlan], bool]:
    def check(plan: ToolPlan) -> bool:
        if plan.protocol_error or len(plan.calls) != 1:
            return False
        if plan.control is None or plan.control.kind != "continue":
            return False
        call = plan.calls[0]
        if call.name != name:
            return False
        for key, value in required_args.items():
            if call.args.get(key) != value:
                return False
        return True

    return check


def _expected_done(plan: ToolPlan) -> bool:
    return (
        not plan.protocol_error
        and not plan.calls
        and plan.control is not None
        and plan.control.kind == "done"
        and bool(plan.control.body.strip())
    )


CASES = (
    RepairCase(
        name="unknown_write_file",
        bad_reply=_json({
            "tool": "write_file",
            "args": {"path": "notes.txt", "content": "hello\n"},
        }),
        context=(
            "The user asked you to create notes.txt containing hello. "
            "No local tool has run yet."
        ),
        valid_reply={
            "tool": "edit",
            "args": {"path": "notes.txt", "content": "hello\n"},
        },
        expected=_expected_call("edit", path="notes.txt", content="hello\n"),
    ),
    RepairCase(
        name="invalid_edit_mixed_modes",
        bad_reply=_json({
            "tool": "edit",
            "args": {
                "path": "app.py",
                "content": "VALUE = 2\n",
                "replacements": [{"old_string": "VALUE = 1\n", "new_string": "VALUE = 2\n"}],
            },
        }),
        context=(
            "The existing app.py was already read and contains exactly: VALUE = 1. "
            "Change it to VALUE = 2."
        ),
        valid_reply={
            "tool": "edit",
            "args": {
                "path": "app.py",
                "old_string": "VALUE = 1\n",
                "new_string": "VALUE = 2\n",
            },
        },
        expected=_expected_call(
            "edit",
            path="app.py",
            replacements=[{"search": "VALUE = 1\n", "replace": "VALUE = 2\n"}],
        ),
    ),
    RepairCase(
        name="invalid_read_offset",
        bad_reply=_json({
            "tool": "read_file",
            "args": {"path": "app.py", "offset": 0, "limit": 120},
        }),
        context="Read app.py from the beginning before deciding whether to edit.",
        valid_reply={
            "tool": "read_file",
            "args": {"path": "app.py", "offset": 1, "limit": 120},
        },
        expected=_expected_call("read", path="app.py", offset=1, limit=120),
    ),
    RepairCase(
        name="direct_answer",
        bad_reply=(
            "I checked the requested change. No files need to be edited, so the "
            "task is complete."
        ),
        context=(
            "The task is complete and no local tool call is needed. Return the "
            "final user-facing answer through done.summary."
        ),
        valid_reply={
            "tool": "done",
            "args": {
                "summary": "I checked the requested change. No files need to be edited."
            },
        },
        expected=_expected_done,
    ),
    RepairCase(
        name="native_tool_denial",
        bad_reply=(
            "The website says read_file is not available, so I cannot inspect app.py."
        ),
        context=(
            "The next correct action is to inspect app.py. Website-native tool "
            "availability is irrelevant because the local runner executes JSON."
        ),
        valid_reply={"tool": "read_file", "args": {"path": "app.py"}},
        expected=_expected_call("read", path="app.py"),
    ),
    RepairCase(
        name="nested_tool_in_done",
        bad_reply=_json({
            "tool": "done",
            "args": {
                "summary": _json({
                    "tool": "run",
                    "args": {"command": "python -m unittest", "path": "."},
                })
            },
        }),
        context=(
            "Files changed and the user asked for verification. The next correct "
            "action is to run the test command, not to finish."
        ),
        valid_reply={
            "tool": "run",
            "args": {"command": "python -m unittest", "path": "."},
        },
        expected=_expected_call("run", path=".", command="python -m unittest"),
    ),
)


def _case_by_name() -> dict[str, RepairCase]:
    return {case.name: case for case in CASES}


def _bad_plan_error(case: RepairCase) -> str:
    plan = _bad_plan(case)
    if plan.protocol_error:
        return plan.protocol_error
    if plan.calls or plan.control is not None:
        return "the previous JSON was structurally valid but semantically wrong for this probe"
    folded = case.bad_reply.lower()
    if "tool" in folded and ("not available" in folded or "does not exist" in folded):
        return "native website tool denial instead of a local-runner JSON command"
    return "no valid JSON tool call found"


def _bad_plan(case: RepairCase) -> ToolPlan:
    return JsonToolCodec().parse(case.bad_reply)


def _baseline_repair(case: RepairCase) -> str:
    codec = JsonToolCodec()
    error = _bad_plan_error(case)
    if error == "no valid JSON tool call found":
        return codec.repair_prompt()
    return f"Protocol error: {error}\n\n{codec.repair_prompt()}"


def _typed_repair(case: RepairCase) -> str:
    return _protocol_repair_prompt(
        JsonToolCodec(),
        _bad_plan(case),
        previous_reply=case.bad_reply,
    )


def _prompt(case: RepairCase, arm: str) -> str:
    repair = _baseline_repair(case) if arm == "baseline" else _typed_repair(case)
    return (
        "Coding protocol repair A/B. This is a standalone continuation of a "
        "local coding-agent conversation.\n\n"
        f"{JsonToolCodec().system_prompt()}\n\n"
        "Task context:\n"
        f"{case.context}\n\n"
        "Previous invalid reply:\n"
        "```text\n"
        f"{case.bad_reply}\n"
        "```\n\n"
        f"{repair}\n\n"
        "Return the repaired next action now. Output exactly one JSON object, "
        "no markdown fences and no other text."
    )


def _strict_single_json_object(reply: str) -> bool:
    text = str(reply or "").strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return False
    return isinstance(value, dict)


def _analyze_reply(case: RepairCase, reply: str) -> dict[str, Any]:
    codec = JsonToolCodec()
    plan = codec.parse(reply)
    objects = _balanced_json_objects(str(reply or ""))
    calls = [{"name": call.name, "args": dict(call.args)} for call in plan.calls]
    control = (
        {"kind": plan.control.kind, "body": plan.control.body}
        if plan.control is not None
        else None
    )
    accepted = bool(not plan.protocol_error and (plan.calls or plan.control is not None))
    expected = case.expected(plan)
    strict_json = _strict_single_json_object(reply)
    single_action = (
        len(objects) == 1
        and (
            (len(plan.calls) == 1 and plan.control is not None and plan.control.kind == "continue")
            or (not plan.calls and plan.control is not None and plan.control.kind == "done")
        )
    )
    clean_repair = bool(accepted and expected and strict_json and single_action)
    return {
        "accepted": accepted,
        "expected_action": expected,
        "strict_single_json": strict_json,
        "single_action": single_action,
        "clean_repair": clean_repair,
        "json_object_count": len(objects),
        "calls": calls,
        "control": control,
        "protocol_error": plan.protocol_error,
        "protocol_error_kind": plan.protocol_error_kind,
        "reply_chars": len(str(reply or "")),
        "reply_excerpt": str(reply or "")[:MAX_REPLY_CHARS],
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
            "accepted": sum(1 for row in arm_rows if row["analysis"].get("accepted")),
            "expected_action": sum(
                1 for row in arm_rows if row["analysis"].get("expected_action")
            ),
            "strict_single_json": sum(
                1 for row in arm_rows if row["analysis"].get("strict_single_json")
            ),
            "clean_repair": sum(
                1 for row in arm_rows if row["analysis"].get("clean_repair")
            ),
        }
    baseline = summary["arms"].get("baseline", {})
    typed = summary["arms"].get("typed", {})
    if baseline.get("count") and typed.get("count"):
        summary["typed_delta_vs_baseline"] = {
            "accepted": int(typed.get("accepted") or 0) - int(baseline.get("accepted") or 0),
            "expected_action": (
                int(typed.get("expected_action") or 0)
                - int(baseline.get("expected_action") or 0)
            ),
            "strict_single_json": (
                int(typed.get("strict_single_json") or 0)
                - int(baseline.get("strict_single_json") or 0)
            ),
            "clean_repair": (
                int(typed.get("clean_repair") or 0)
                - int(baseline.get("clean_repair") or 0)
            ),
        }
    return summary


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _default_output(provider: str) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return RESULTS_DIR / f"coding_repair_prompt_ab-{provider}-{stamp}.json"


def _select_arms(values: list[str]) -> tuple[str, ...]:
    found: list[str] = []
    raw_values = values or ["baseline,typed"]
    for value in raw_values:
        for item in str(value or "").split(","):
            arm = item.strip().lower()
            if arm and arm not in found:
                found.append(arm)
    unknown = [arm for arm in found if arm not in ARMS]
    if unknown:
        raise SystemExit(f"unknown arm(s): {', '.join(unknown)}")
    return tuple(found or ARMS)


def _select_cases(values: list[str]) -> tuple[RepairCase, ...]:
    index = _case_by_name()
    if not values:
        return CASES
    names: list[str] = []
    for value in values:
        for item in str(value or "").split(","):
            name = item.strip()
            if name and name not in names:
                names.append(name)
    unknown = [name for name in names if name not in index]
    if unknown:
        raise SystemExit(f"unknown case(s): {', '.join(unknown)}")
    return tuple(index[name] for name in names)


def _self_test() -> None:
    for case in CASES:
        for arm in ARMS:
            prompt = _prompt(case, arm)
            assert JsonToolCodec().system_prompt() in prompt
            assert case.bad_reply in prompt
            assert "exactly one JSON object" in prompt
        typed = _typed_repair(case)
        assert "Preserve the previous intended path" in typed
        if case.name == "unknown_write_file":
            assert _json(case.valid_reply) in typed
            assert "new_app.py" not in typed
        if case.name == "invalid_edit_mixed_modes":
            assert "VALUE = 1\\n" in typed
            assert "VALUE = 2\\n" in typed
        if case.name == "invalid_read_offset":
            assert _json(case.valid_reply) in typed
            assert '"limit":300' not in typed
        good = _analyze_reply(case, _json(case.valid_reply))
        assert good["accepted"], (case.name, good)
        assert good["expected_action"], (case.name, good)
        assert good["clean_repair"], (case.name, good)
    nested = next(case for case in CASES if case.name == "nested_tool_in_done")
    bad = _analyze_reply(nested, nested.bad_reply)
    assert not bad["accepted"]
    assert "done summary" in bad["protocol_error"]
    mixed = next(case for case in CASES if case.name == "invalid_edit_mixed_modes")
    assert "exactly one mode" in _bad_plan_error(mixed)
    print("self-test ok")


def run_live(
    *,
    provider_id: str,
    port: int,
    timeout: float,
    new_chat_timeout: float,
    fresh: bool,
    keep_open: bool,
    arms: tuple[str, ...],
    cases: tuple[RepairCase, ...],
    output: Path,
) -> int:
    payload: dict[str, Any] = {
        "probe": "coding_repair_prompt_ab",
        "provider": provider_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "arms": list(arms),
        "cases": [case.name for case in cases],
        "rows": [],
        "summary": {},
    }
    _atomic_write_json(output, payload)
    provider_controls.begin_task_context(f"coding-repair-prompt-ab:{provider_id}")
    provider = None
    try:
        provider = (
            connect_fresh_provider_tab(provider_id, port=port)
            if fresh
            else connect_provider(provider_id, port=port)
        )
        for case in cases:
            for arm in arms:
                prompt = _prompt(case, arm)
                started = time.time()
                row: dict[str, Any] = {
                    "case": case.name,
                    "arm": arm,
                    "prompt_chars": len(prompt),
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
                try:
                    provider.new_chat(timeout=new_chat_timeout)
                    reply = provider.send(prompt, timeout=timeout)
                    row.update({
                        "ok": True,
                        "seconds": round(time.time() - started, 3),
                        "analysis": _analyze_reply(case, reply),
                    })
                except Exception as exc:
                    row.update({
                        "ok": False,
                        "seconds": round(time.time() - started, 3),
                        "error": f"{type(exc).__name__}: {exc}",
                    })
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
            try:
                provider.close()
            except Exception:
                pass
        provider_controls.end_task_context()


def main(argv: list[str] | None = None) -> int:
    choices = tuple(provider_id for provider_id in provider_ids() if provider_id != "local")
    parser = argparse.ArgumentParser(description="Live A/B for coding protocol repair prompts.")
    parser.add_argument("--provider", choices=choices, default="mimo")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--new-chat-timeout", type=float, default=45.0)
    parser.add_argument("--arm", action="append", default=[], help="arm or comma list")
    parser.add_argument("--case", action="append", default=[], help="case or comma list")
    parser.add_argument("--fresh", action="store_true", help="open an isolated fresh provider tab")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        _self_test()
        return 0

    arms = _select_arms(args.arm)
    cases = _select_cases(args.case)
    output = args.output or _default_output(args.provider)
    return run_live(
        provider_id=args.provider,
        port=args.port,
        timeout=args.timeout,
        new_chat_timeout=args.new_chat_timeout,
        fresh=args.fresh,
        keep_open=args.keep_open,
        arms=arms,
        cases=cases,
        output=output,
    )


if __name__ == "__main__":
    raise SystemExit(main())