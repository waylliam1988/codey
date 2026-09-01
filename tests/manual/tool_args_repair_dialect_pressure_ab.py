"""Dialect-pressure live A/B probe for Tool Argument Repair.

This is not a natural production-yield measurement. It deliberately asks a
real provider, through the production agent loop, to emit common argument
variants such as pattern, old/new, cmd, and numeric-string offsets. Use the
natural live harness for ordinary provider behavior; use this one to verify the
production loop absorbs dialect-shaped arguments when they appear.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.providers import controls as provider_controls
from codey.providers.registry import DEFAULT_PROVIDER_ID, connect_provider, provider_ids
from codey.protocols.json_codec import PROTOCOL_INVALID_ARGS, JsonToolCodec
from tests.manual.tool_args_repair_live_ab import (
    ARMS,
    Baseline052Codec,
    Case,
    _atomic_write_json,
    _run_agent_case,
    _summarize,
)


RESULTS_DIR = Path(__file__).resolve().parent / "results"
PROBE = "tool_args_repair_dialect_pressure_ab"
PROBE_SLUG = "tool-args-repair-dialect-pressure-ab"


PRESSURE_CASES = (
    Case(
        name="search_read_numeric_pressure",
        task=(
            "Dialect-pressure probe. First call grep to find TARGET_TOKEN under "
            "src, and intentionally put the search text in args.pattern instead "
            "of args.query. Then call read_file for the matching file and "
            "intentionally serialize offset and limit as JSON strings, for "
            "example \"1\" and \"20\". Finish with the matching file path."
        ),
        files={
            "src/auth/service.py": "HEADER = 'ok'\nTARGET_TOKEN = 'alpha'\n",
            "README.md": "# Fixture\n",
        },
        expected_final={
            "src/auth/service.py": "HEADER = 'ok'\nTARGET_TOKEN = 'alpha'\n",
            "README.md": "# Fixture\n",
        },
    ),
    Case(
        name="edit_run_alias_pressure",
        task=(
            "Dialect-pressure probe. Change only FEATURE_FLAG from False to True "
            "in config.py. When calling edit, intentionally use args.old and "
            "args.new instead of args.old_string and args.new_string. Then run "
            "python -m py_compile config.py and intentionally put the command in "
            "args.cmd instead of args.command. Finish only after the run result."
        ),
        files={"config.py": "FEATURE_FLAG = False\n"},
        expected_final={"config.py": "FEATURE_FLAG = True\n"},
    ),
)


DIALECT_SAMPLES = (
    (
        "pattern",
        '{"tool":"grep","args":{"pattern":"TARGET_TOKEN","path":"src"}}',
        "search_field_alias",
    ),
    (
        "numeric_strings",
        '{"tool":"read_file","args":{"path":"src/auth/service.py","offset":"1","limit":"20"}}',
        "numeric_coerced",
    ),
    (
        "old_new",
        '{"tool":"edit","args":{"path":"config.py","old":"FEATURE_FLAG = False","new":"FEATURE_FLAG = True"}}',
        "edit_field_alias",
    ),
    (
        "cmd",
        '{"tool":"run","args":{"cmd":"python -m py_compile config.py","path":"."}}',
        "command_field_alias",
    ),
)


def _default_output(provider_id: str) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return RESULTS_DIR / f"{PROBE}-{provider_id}-{stamp}.json"


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
    index = {case.name: case for case in PRESSURE_CASES}
    if not values:
        return PRESSURE_CASES
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


def _summarize_pressure(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = _summarize(rows)
    candidate_rows = [
        row for row in rows if row.get("arm") == "candidate" and row.get("ok")
    ]
    baseline_rows = [
        row for row in rows if row.get("arm") == "baseline" and row.get("ok")
    ]
    summary["pressure"] = {
        "candidate_cases_with_alias_rewrites": sum(
            1
            for row in candidate_rows
            if int(row.get("protocol", {}).get("alias_rewrite_count") or 0) > 0
        ),
        "baseline_cases_with_protocol_errors": sum(
            1
            for row in baseline_rows
            if int(row.get("protocol", {}).get("protocol_error_count") or 0) > 0
        ),
    }
    return summary


def _self_test() -> None:
    baseline = Baseline052Codec()
    candidate = JsonToolCodec()
    for name, sample, repair_kind in DIALECT_SAMPLES:
        base = baseline.parse(sample)
        current = candidate.parse(sample)
        assert base.protocol_error_kind == PROTOCOL_INVALID_ARGS, name
        assert current.protocol_error == "", (name, current.protocol_error)
        assert current.arg_repair_counts.get(repair_kind, 0) > 0, name
    for case in PRESSURE_CASES:
        assert "Dialect-pressure probe" in case.task
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
        "probe": PROBE,
        "provider": provider_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "arms": list(arms),
        "cases": [case.name for case in cases],
        "note": (
            "Dialect-pressure suite: forced-alias prompts verify absorption "
            "when provider-shaped arguments appear; do not report this as "
            "natural production turn savings."
        ),
        "rows": [],
        "summary": {},
    }
    _atomic_write_json(output, payload)
    provider_controls.begin_task_context(f"{PROBE_SLUG}:{provider_id}")
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
                    probe_slug=PROBE_SLUG,
                )
                payload["rows"].append(row)
                payload["summary"] = _summarize_pressure(payload["rows"])
                _atomic_write_json(output, payload)
                print(json.dumps(row, ensure_ascii=False), flush=True)
        payload["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        payload["summary"] = _summarize_pressure(payload["rows"])
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
    parser = argparse.ArgumentParser(
        description="Dialect-pressure live provider A/B for 0.5.3 tool argument repair."
    )
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
