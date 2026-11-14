"""Match declared browser wait predicates to executable leaf arguments."""
from __future__ import annotations

from dataclasses import dataclass

from codey.policies.tool_requirement import argument_digest, parse_tool_success_check
from codey.toolchain.tool_spec import validate_args_against_spec
from codey.types.json_value import JsonObject, parse_json_object


@dataclass(frozen=True)
class BrowserWaitPlan:
    waits: tuple[JsonObject, ...]
    missing: tuple[JsonObject, ...]

def wait_plan(checks: tuple[str, ...], supplied: tuple[JsonObject, ...] = ()) -> BrowserWaitPlan:
    required: list[JsonObject] = []
    waits: list[JsonObject] = []
    for check in checks:
        rule = parse_tool_success_check(check)
        if rule is None or rule.tool != "browser_wait":
            continue
        expected = parse_json_object(rule.arguments_json)
        required.append(expected)
        args = dict(expected)
        args.setdefault("timeout_ms", 5000)
        if (isinstance(args.get("condition"), str) and isinstance(args.get("value"), str)
                and args["value"] and not validate_args_against_spec("browser_wait", args) and args not in waits):
            waits.append(args)
    for value in supplied:
        args = dict(value)
        args.setdefault("timeout_ms", 5000)
        if error := validate_args_against_spec("browser_wait", args):
            raise ValueError(error)
        if args not in waits:
            waits.append(args)
    missing = tuple(expected for expected in required if not any(all(
        key in wait and argument_digest(wait[key]) == argument_digest(value) for key, value in expected.items())
        for wait in waits))
    return BrowserWaitPlan(tuple(waits), missing)
