"""Offer browser controls within the frozen tool authorization."""
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Protocol

from codey.operations.browser import browser_followups
from codey.operations.kernel.turn_presentation import refresh_snapshot_contract

if TYPE_CHECKING:
    from codey.operations.kernel.turn_contract import TurnSnapshot
    from codey.types.json_value import ToolArgs


class BrowserOfferSession(Protocol):
    @property
    def task_text(self) -> str: ...

    @property
    def model_tool_names(self) -> tuple[str, ...] | None: ...

    @property
    def controller_denied(self) -> tuple[str, ...]: ...


def offer_browser_tools(snapshot: TurnSnapshot, session: BrowserOfferSession) -> TurnSnapshot:
    from codey.toolchain.browser_native_specs import browser_native_specs

    exposed = session.model_tool_names
    direct_names = tuple(name for name in (*browser_followups.DIRECT_TOOLS, "browser_wait", *browser_native_specs())
        if browser_followups.allows_surface(snapshot, (name,))
        and name not in session.controller_denied and (exposed is None or name in exposed))
    if not direct_names:
        return snapshot
    browser = replace(snapshot.browser, followups=True)
    if browser_followups.codemode_only(session.task_text):
        return refresh_snapshot_contract(replace(snapshot, browser=browser))
    names = (*snapshot.tool_names, *(name for name in direct_names if name not in snapshot.tool_names))
    offered = replace(snapshot, tool_names=names,
                      browser=replace(browser, fill_text=browser_followups.explicit_fill_text(session.task_text)))
    return refresh_snapshot_contract(offered)


def offer_browser_call(snapshot: TurnSnapshot, session: BrowserOfferSession, name: str, *, research: bool = False) -> TurnSnapshot:
    """Offer an existing leaf through the same single-step CodeMode lowering."""
    eligible = set(browser_followups.FOLLOWUP_TOOLS)
    if (name not in eligible
            or not browser_followups.allows_surface(snapshot, (name,), research=research)
            or name in session.controller_denied
            or (session.model_tool_names is not None and name not in session.model_tool_names)):
        return snapshot
    browser = replace(snapshot.browser, followups=True)
    if browser_followups.codemode_only(session.task_text):
        return refresh_snapshot_contract(replace(snapshot, browser=browser))
    return refresh_snapshot_contract(replace(snapshot, browser=browser,
        tool_names=(*snapshot.tool_names, *((name,) if name not in snapshot.tool_names else ()))))


def direct_browser_call(name: str, args: ToolArgs, snapshot: TurnSnapshot) -> bool:
    from codey.policies.tool_requirement import parse_tool_success_check

    checks = snapshot.policy.required_checks if snapshot.policy else ()
    bound_observation = name in {"browser_snapshot", "browser_read"} and any(
        (rule := parse_tool_success_check(check)) is not None and rule.tool == name and rule.via == "codemode"
        for check in checks)
    return snapshot.browser.followups and (
        name in {"browser_fill", "browser_click", *browser_followups.SINGLE_STEP_TOOLS} or bound_observation
        or isinstance(args.get("ref"), dict) and snapshot.browser.references is not None
        or name == "browser_navigate" and snapshot.navigation_observes
        or name == "browser_navigate" and snapshot.browser.navigation_url is not None
        or snapshot.research.scope is not None and name in {"browser_snapshot", "browser_read", "browser_navigate"})


def lower_browser_call(name: str, args: ToolArgs, snapshot: TurnSnapshot) -> ToolArgs:
    if name == "browser_fill":
        inputs = browser_followups.fill_args(args, snapshot.browser.fill_text)
        code = browser_followups.DIRECT_REF + browser_followups.FILL + browser_followups.OBSERVE
    elif name == "browser_navigate" and snapshot.navigation_observes:
        inputs = dict(args)
        verify = snapshot.browser.navigation_verifies and (args.get("target") == "task" or args.get("url") == snapshot.browser.navigation_url)
        code = browser_followups.NAVIGATE_VERIFY if verify else browser_followups.NAVIGATE
    elif name == "browser_wait" and args.get("condition") == "text_changed" and args.get("run_in_background") is not True:
        inputs = dict(args)
        code = "const action=unwrap(await tools.browser_wait(inputs));" + browser_followups.OBSERVE
    else:
        inputs = dict(args)
        code = f"return unwrap(await tools.{name}(inputs));"
    return {"code": code, "inputs": inputs}


def lower_browser_binding(kind: str, args: ToolArgs, snapshot: TurnSnapshot) -> ToolArgs:
    if kind == "upload":
        from codey.operations.browser.browser_upload import lower_upload_action

        return lower_upload_action(args, snapshot.policy.required_checks if snapshot.policy else ())
    if kind != "browser_copy" or snapshot.browser.copy is None:
        raise ValueError("browser copy input has no captured observation")
    return snapshot.browser.copy.lower(args)
