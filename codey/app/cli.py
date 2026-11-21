"""Codey entry point.

    python -m codey            launch the native UI (default)
    python -m codey ui         same as above, with --port
    python -m codey chat ...   single-shot prompt, prints reply
    python -m codey agent ...  agent loop without UI (CLI mode)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Protocol, TextIO, runtime_checkable

from codey.policies.capabilities import BROWSER_CAPABILITIES


class _Arguments(argparse.Namespace):
    cmd: str
    func: object
    state_home: str
    port: int
    provider: str
    prompt: list[str]
    timeout: float
    readonly: bool
    auto: bool
    intent: str
    task: list[str]
    project: str
    json: bool
    max_turns: int | None
    session_id: str
    continue_task: bool
    review_source_run_id: str
    review_policy: str | None
    allow_web: bool | None
    allow_browser: bool
    allow_write: bool
    output_format: str
    output_schema: str
    model: str
    effort: str | None
    ghost_cmd: str
    kind: str
    text: str
    proposal_id: str
    version: str
    scope_kind: str
    request_id: str
    control: str
    enabled: str
    item_id: str
    expires_at: str
    slot: str
    context: str
    supersedes: list[str]
    query: str
    run_id: str
    rating: str
    statement: str
    due_at: str
    prediction_id: str
    feedback_id: str
    as_of: str
    fields: str
    value: str


@runtime_checkable
class _Command(Protocol):
    def __call__(self, args: _Arguments) -> int: ...


def _safe_print(value: object, *, file: TextIO | None = None) -> None:
    if file is None:
        file = sys.stdout
    text = str(value)
    declared_encoding: object = getattr(file, "encoding", None)
    encoding = declared_encoding if isinstance(declared_encoding, str) and declared_encoding else "utf-8"
    safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    file.write(safe + "\n")


def cmd_ui(args: _Arguments) -> int:
    from codey.app.desktop import open_desktop
    from codey.storage.local_store import DEFAULT_STATE_HOME

    open_desktop(Path(args.state_home) if args.state_home else DEFAULT_STATE_HOME, port=args.port)
    return 0


def cmd_chat(args: _Arguments) -> int:
    from codey.providers import controls as provider_controls
    from codey.providers.registry import connect_provider

    prompt = " ".join(args.prompt)
    _safe_print(f"[codey] connecting provider ({args.provider}) ...", file=sys.stderr)
    provider_controls.begin_task_context(f"cli-chat:{args.provider}")
    provider = None
    try:
        provider = connect_provider(args.provider, port=args.port)
        reply = provider.send(prompt, timeout=args.timeout)
    finally:
        try:
            if provider is not None:
                provider.close()
        finally:
            provider_controls.end_task_context()
    _safe_print(reply)
    return 0


def _agent_intent(args: _Arguments) -> str:
    if args.readonly:
        return "planning_readonly"
    if args.auto:
        return "auto"
    return args.intent or "project"


def cmd_agent(args: _Arguments) -> int:
    task = " ".join(args.task)
    project = Path(args.project).resolve() if args.project else None
    json_mode = args.json is True
    _safe_print(f"[codey] project: {project}", file=sys.stderr)
    from codey.agents.request import DEFAULT_MAX_TURNS
    from codey.app.headless_runner import (
        HeadlessRequest,
        emit_jsonl,
        run_headless,
    )
    from codey.storage.local_store import DEFAULT_STATE_HOME

    state_home_arg = args.state_home
    state_home = (
        Path(state_home_arg).expanduser()
        if state_home_arg
        else DEFAULT_STATE_HOME
    )
    raw_max_turns = args.max_turns
    max_turns = DEFAULT_MAX_TURNS if raw_max_turns is None else raw_max_turns
    request = HeadlessRequest(
        project=project,
        task=task,
        provider_id=args.provider,
        max_turns=max_turns,
        intent=_agent_intent(args),
        state_home=state_home,
        port=args.port,
        session_id=args.session_id,
        review_source_run_id=args.review_source_run_id,
        allow_web=args.allow_web,
        requested_capabilities=tuple(name for name, enabled in (
            ("project.write", args.allow_write),
            *((name, args.allow_browser) for name in BROWSER_CAPABILITIES),
        ) if enabled),
        continue_task=args.continue_task,
        review_policy=args.review_policy,
        output_format=args.output_format, output_schema=args.output_schema,
        model_selection={"model": args.model, **({"effort": args.effort} if args.effort is not None else {})} if args.model else {},
    )
    if json_mode:
        result = run_headless(
            request,
            emit_jsonl=lambda payload: emit_jsonl(payload, file=sys.stdout),
        )
        return result.exit_code
    rows: list[dict[str, object]] = []

    def _emit_progress(row: dict[str, object]) -> None:
        rows.append(row)
        line = _human_cli_line(row)
        if line:
            _safe_print(line, file=sys.stderr)

    result = run_headless(request, emit_jsonl=_emit_progress)
    done = next(
        (row for row in reversed(rows) if str(row.get("type") or "") == "task_done"),
        None,
    )
    _safe_print(str((done or {}).get("summary") or ""), file=sys.stdout)
    return result.exit_code


def _human_cli_line(row: dict[str, object]) -> str:
    """One quiet human line per headless event; nothing machine-shaped."""
    kind = str(row.get("type") or "")
    if kind == "task_start":
        return f"[codey] start mode={row.get('mode', '')} provider={row.get('provider', '')}"
    if kind in {"status", "info", "review"}:
        text = str(row.get("status") or row.get("text") or "").strip()
        return f"[codey] {text}" if text else ""
    if kind in {"tool_started", "tool"}:
        tool = str(row.get("tool_name") or row.get("tool") or "")
        path = str(row.get("path") or "")
        extra = f" {path}" if path else ""
        command = str(row.get("command") or "")
        if command:
            extra += f" $ {command[:120]}"
        status = "" if kind == "tool_started" else (
            "ok" if row.get("ok") is True else "failed")
        return f"[turn {row.get('turn', '')}] {tool}{extra} {status}".rstrip()
    if kind in {"shell_request", "shell_rejected"}:
        return "[codey] shell commands need approval; denied headlessly."
    return ""


def cmd_ghost(args: _Arguments) -> int:
    from codey.app.context import AppContext
    from codey.app.memory_routes import memory_action
    from codey.storage.local_store import DEFAULT_STATE_HOME

    home: str = args.state_home
    state = AppContext(Path(home).expanduser() if home else DEFAULT_STATE_HOME, start_scheduler=False)
    try:
        action: str = args.ghost_cmd
        if action == "maintain":
            return 0 if state.memory.maintain(as_of=args.as_of or state.memory.clock()) else 1
        if action == "export":
            _safe_print(json.dumps(state.memory.export_state(), ensure_ascii=False, indent=2))
            return 0
        body: dict[str, object] = {"action": action, "session_id": args.session_id, "project": args.project}
        if args.scope_kind:
            body["scope_kind"] = args.scope_kind
        if args.request_id:
            body["request_id"] = args.request_id
        if action == "propose":
            body.update(kind=args.kind, text=args.text, expires_at=args.expires_at,
                        slot=args.slot, value=args.value, context=args.context, supersedes=args.supersedes)
        elif action in {"accept", "reject"}:
            body.update(proposal_id=args.proposal_id, version=args.version)
        elif action == "control":
            body.update(control=args.control, enabled=args.enabled == "on")
        elif action in {"revoke", "quarantine", "restore"}:
            body["item_id"] = args.item_id
        elif action == "explain":
            body.update(query=args.query, context=args.context)
        elif action == "explain_run":
            body["run_id"] = args.run_id
        elif action in {"feedback", "predict"}:
            body.update(item_id=args.item_id, rating=args.rating)
            if action == "feedback":
                body["run_id"] = args.run_id
            else:
                body.update(statement=args.statement, due_at=args.due_at)
        elif action == "review":
            body.update(prediction_id=args.prediction_id, feedback_id=args.feedback_id)
        from codey.types.json_value import parse_json_object

        extra = parse_json_object(args.fields)
        reserved = ("action", "session_id", "project", "scope_kind", "request_id")
        if any(key in extra for key in reserved):
            raise ValueError("--fields cannot override command identity or scope")
        body.update(extra)
        status, payload = memory_action(state, body)
        _safe_print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if status == 200 else 1
    finally:
        state.close()


def _add_ghost_subcommands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from codey.memory.contracts import MEMORY_CONTROLS

    for action in ("export", "propose", "accept", "reject", "delete_scope", "reset", "maintain", "rebuild",
                   "control", "revoke", "quarantine", "restore", "explain", "explain_run", "feedback", "predict", "review", "family", "compact", "evaluate", "repair"):
        command = sub.add_parser(action)
        command.add_argument("--state-home", default="")
        command.add_argument("--project", default="")
        command.add_argument("--session-id", default="")
        command.add_argument("--scope-kind", choices=("user", "project", "session"), default="")
        command.add_argument("--request-id", default="")
        command.add_argument("--fields", default="{}", help="additional typed memory command fields as JSON")
        if action == "propose":
            command.add_argument("kind", choices=("preference", "goal", "hypothesis"))
            command.add_argument("text")
            command.add_argument("--expires-at", default="")
            command.add_argument("--slot", default="")
            command.add_argument("--value", default="")
            command.add_argument("--context", default="")
            command.add_argument("--supersedes", action="append", default=list[str]())
        if action in {"accept", "reject"}:
            command.add_argument("proposal_id")
            command.add_argument("version")
        if action == "control":
            command.add_argument("control", choices=MEMORY_CONTROLS)
            command.add_argument("enabled", choices=("on", "off"))
        if action in {"revoke", "quarantine", "restore", "feedback", "predict"}:
            command.add_argument("item_id")
        if action == "explain":
            command.add_argument("query", nargs="?", default="")
            command.add_argument("--context", default="")
        if action in {"feedback", "predict"}:
            command.add_argument("--rating", choices=("helpful", "unhelpful", "correction"), default="helpful")
        if action in {"feedback", "explain_run"}:
            command.add_argument("--run-id", required=True)
        if action == "predict":
            command.add_argument("--statement", required=True)
            command.add_argument("--due-at", required=True)
        if action == "review":
            command.add_argument("prediction_id")
            command.add_argument("--feedback-id", default="")
        if action == "maintain":
            command.add_argument("--as-of", default="")
        command.set_defaults(func=cmd_ghost)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    # Default: if no subcommand given, launch the UI.
    if not argv or argv[0] not in {"ui", "chat", "agent", "ghost", "serve", "control", "rpc", "mcp", "-h", "--help"}:
        argv = ["ui", *argv]

    # Single parser tree: ghost shares the root parser so its registration
    # exists exactly once. Parser build stays cold (no toolchain import);
    # DEFAULT_MAX_TURNS is resolved lazily in cmd_agent.
    from codey.providers.catalog import DEFAULT_PROVIDER_ID, provider_ids

    ap = argparse.ArgumentParser(prog="codey")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp_ui = sub.add_parser("ui", help="launch the native UI (default)")
    sp_ui.add_argument("--port", type=int, default=5173)
    sp_ui.add_argument("--state-home", default="")
    sp_ui.set_defaults(func=cmd_ui)

    sp_chat = sub.add_parser("chat", help="single-shot prompt")
    sp_chat.add_argument("prompt", nargs="+")
    sp_chat.add_argument("--port", type=int, default=9222)
    sp_chat.add_argument("--provider", choices=provider_ids(), default=DEFAULT_PROVIDER_ID)
    sp_chat.add_argument("--timeout", type=float, default=300.0)
    sp_chat.set_defaults(func=cmd_chat)

    sp_agent = sub.add_parser("agent", help="CLI agent loop")
    sp_agent.add_argument("--project", default="", help="attach a project folder")
    sp_agent.add_argument("--max-turns", type=int, default=None)
    sp_agent.add_argument("--output-format", choices=("text", "json"), default="text")
    sp_agent.add_argument("--output-schema", default="", help="JSON Schema for a JSON final answer")
    sp_agent.add_argument("--port", type=int, default=9222)
    sp_agent.add_argument("--provider", choices=provider_ids(), default=DEFAULT_PROVIDER_ID)
    sp_agent.add_argument("--model", default="", help="explicit API model; protocol comes from connection settings or catalog")
    sp_agent.add_argument("--effort", choices=("off", "minimal", "low", "medium", "high", "xhigh", "max"), default=None)
    sp_agent.add_argument("--json", action="store_true", help="emit JSONL events on stdout")
    modes = sp_agent.add_mutually_exclusive_group()
    modes.add_argument("--intent", choices=("auto", "chat", "project", "hybrid", "research", "review", "planning_readonly"), help="task mode")
    modes.add_argument("--readonly", action="store_true", help="read-only planning")
    modes.add_argument("--auto", action="store_true", help="choose the task path automatically")
    web = sp_agent.add_mutually_exclusive_group()
    web.add_argument("--allow-web", action="store_true", default=None, help="authorize web reading")
    web.add_argument("--no-web", action="store_false", dest="allow_web", help="disable Web tools for this task")
    web.add_argument("--allow-browser", action="store_true", help="authorize browser tools")
    sp_agent.add_argument("--allow-write", action="store_true", help="authorize project writing, including Research")
    sp_agent.add_argument("--session-id", default="", help="existing or new task session")
    sp_agent.add_argument("--continue", dest="continue_task", action="store_true", help="continue the selected session")
    sp_agent.add_argument("--review-source-run-id", default="", help="prefer a verified review from this prior run")
    sp_agent.add_argument("--review-policy", choices=("web_if_available", "require_web"), default=None, help="Reviewer policy; defaults to configured policy")
    sp_agent.add_argument("--state-home", default="", help="local Codey state directory")
    sp_agent.add_argument("task", nargs="+")
    sp_agent.set_defaults(func=cmd_agent)

    sp_ghost = sub.add_parser("ghost", help="inspect and control Ghost memory inbox")
    sp_ghost_sub = sp_ghost.add_subparsers(dest="ghost_cmd", required=True)
    _add_ghost_subcommands(sp_ghost_sub)

    from codey.control.cli import register

    register(sub)

    args = ap.parse_args(argv, namespace=_Arguments())
    if args.cmd == "agent":
        from codey.providers.catalog import API_CONNECTIONS

        if args.model and args.provider not in API_CONNECTIONS:
            sp_agent.error("--model requires an API connection")
        if args.effort is not None and not args.model:
            sp_agent.error("--effort requires --model")
        intent = _agent_intent(args)
        if args.continue_task and not args.session_id:
            sp_agent.error("--continue requires --session-id")
        if args.review_source_run_id:
            if not args.session_id:
                sp_agent.error("--review-source-run-id requires --session-id")
            if intent not in {"project", "review"}:
                sp_agent.error("review source requires project or review intent")
    handler: object = args.func
    if not isinstance(handler, _Command):
        raise TypeError("invalid command handler")
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
