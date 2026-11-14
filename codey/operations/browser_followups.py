"""Frozen Chat browser inputs and fixed follow-ups over the existing leaves."""
from __future__ import annotations

import re
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import replace
from typing import TYPE_CHECKING

from codey.operations.browser_upload import upload_wait_plan
from codey.policies.browser_requirement import BrowserPageRounds, parse_browser_requirement
from codey.runtime.core.models import ToolResult
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters, thaw_schema_value
from codey.types.json_value import JsonObject, JsonValue, decode_json_object

if TYPE_CHECKING:
    from codey.operations.kernel_protocol import TurnSnapshot

DIRECT_TOOLS = ("browser_snapshot", "browser_fill", "browser_click", "browser_read")
PURPOSES = ("result", "controls", "action")
TEXT_SOURCE = "task_text"
MAX_TEXT = 4096
PURPOSE_DESCRIPTION = (
    "For a query or detail needed to answer, use purpose=result when offered to read the result. "
    "For a menu or form needed for another action, use controls to observe new refs. "
    "Use action when the click itself is the requested outcome. Only declared purpose values are available."
    " Returns the click result; follow-up observations are delivered in child receipts. "
    "A failed follow-up returns failure even when the click was delivered."
)
PENDING = "Click delivered. The query result is unconfirmed. Read the result again without repeating the click."
UNCERTAIN = "Click delivery is uncertain. Inspect the page before taking another action."


def explicit_fill_text(task: str) -> str | None:
    header, delimiter, tail = task.partition("\n")
    if not delimiter or not re.search(r"填|录入|输入", header) or re.search(r"不要|禁止|无需|不用|别填|别输入|别录入", header):
        return None
    literal = "备注" in header and ("原文" in header or "原样" in header)
    if literal and "代码块" in header:
        match = re.fullmatch(r"```(?:text)?\r?\n(?P<body>[\s\S]+?)\r?\n```(?:\r?\n)?", tail)
        if match is None or re.search(r"(?m)^\s*```", match["body"]):
            return None
        tail = match["body"]
    elif literal and re.search(r"(?:以下|下面)(?:两|2)行", header):
        if not re.fullmatch(r"[^\r\n]+\r?\n[^\r\n]+", tail):
            return None
    elif re.search(r"保留(?:双)?引号和换行(?:填入|录入|输入)这段备注", header):
        lines = tail.splitlines()
        if len(lines) < 2 or any(not line.strip() or line.lstrip().startswith("```") for line in lines):
            return None
    else:
        return None
    return tail if tail.strip() and len(tail) <= MAX_TEXT else None


def model_spec(spec: ToolSpec, fill_text: str | None, *, click_purposes: tuple[str, ...] = PURPOSES) -> ToolSpec:
    if spec.name == "browser_fill":
        fields = decode_json_object(thaw_schema_value(dict(spec.parameters)))
        required = spec.required
        detail = "After verified fill, use the automatically returned new snapshot refs."
        if fill_text is not None:
            del fields["value"]
            fields["source"] = {"type": "string", "enum": [TEXT_SOURCE]}
            required = ("ref", "source")
            detail += " Choose source=task_text to fill the original user text verbatim."
        return freeze_spec_parameters(replace(spec, parameters=tuple(fields.items()), required=required,
                                             description=spec.description + " " + detail, json_examples=()))
    if spec.name == "browser_click":
        return freeze_spec_parameters(replace(spec, parameters=(*spec.parameters, (
            "purpose", {"type": "string", "enum": list(click_purposes)})),
            description=spec.description + " " + PURPOSE_DESCRIPTION))
    return spec


def allows_surface(snapshot: TurnSnapshot) -> bool:
    policy = snapshot.policy
    names = {"codemode", *DIRECT_TOOLS}
    return (policy is not None and not policy.strict_research and snapshot.research_scope is None
            and snapshot.browser_copy is None
            and snapshot.input_binding is None
            and "codemode" in snapshot.tool_names and set(DIRECT_TOOLS) <= set(snapshot.nested_tool_names)
            and (snapshot.allowed is None or names <= set(snapshot.allowed))
            and names <= {spec.name for spec in snapshot.frozen_specs if policy.allows(spec.grant)})


OBSERVE = "unwrap(await tools.browser_snapshot({}));return action;"
FILL = "const action=unwrap(await tools.browser_fill({ref,value:inputs.value}));"
DIRECT_REF = "const ref=inputs.ref;"
query_click: ContextVar[bool] = ContextVar("query_click", default=False)


def click_waits(checks: tuple[str, ...], wait_allowed: bool) -> list[JsonObject]:
    plan = upload_wait_plan(checks)
    waits = list(plan.waits)
    for check in checks:
        rule = parse_browser_requirement(check)
        if isinstance(rule, BrowserPageRounds):
            args: JsonObject = {"condition": rule.condition, "value": rule.value, "timeout_ms": 5000}
            if args not in waits:
                waits.append(args)
    if plan.missing or (waits and not wait_allowed):
        raise ValueError("The declared query wait is unavailable in this turn.")
    return waits


def click_plans(checks: tuple[str, ...], names: set[str]) -> JsonObject:
    """Freeze executable leaf sequences; the guest only dispatches these RPCs."""
    plans: JsonObject = {"action": []}
    if "browser_snapshot" in names:
        plans["controls"] = [{"name": "browser_snapshot", "args": {}}]
    if "browser_read" in names:
        try:
            waits = click_waits(checks, "browser_wait" in names)
        except ValueError:
            return plans
        steps: list[JsonValue] = [{"name": "browser_wait", "args": wait} for wait in waits]
        steps.append({"name": "browser_read", "args": {}})
        plans["result"] = steps
    return plans


def fill_args(args: JsonObject, original: str | None) -> JsonObject:
    if "source" not in args:
        return dict(args)
    if args["source"] != TEXT_SOURCE or original is None:
        raise ValueError("The original fill text is not available in this turn.")
    return {**{key: value for key, value in args.items() if key != "source"}, "value": original}


def delivery(result: ToolResult) -> object:
    value = result.canonical.get("value")
    return value.get("delivery") if isinstance(value, dict) else None


def pending_click(results: Mapping[str, ToolResult]) -> tuple[str, ToolResult] | None:
    confirmed: set[str] = set()
    for ref, receipt in reversed(tuple(results.items())):
        if receipt.call.name == "browser_read" and receipt.ok and isinstance(click := receipt.audit.get("query_click"), str):
            confirmed.add(click)
        if ("query_baseline" in receipt.audit and receipt.call.name == "browser_click"
                and ref not in confirmed and delivery(receipt) in ("delivered", "uncertain")):
            return ref, receipt
    return None
