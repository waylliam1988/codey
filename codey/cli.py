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


def _safe_print(value, *, file=sys.stdout) -> None:
    text = str(value)
    encoding = getattr(file, "encoding", None) or "utf-8"
    safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    file.write(safe + "\n")


def cmd_ui(args: argparse.Namespace) -> int:
    from codey.server import serve

    serve(host="127.0.0.1", port=args.port)
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    from codey import provider_controls
    from codey.providers import connect_provider

    prompt = " ".join(args.prompt)
    _safe_print("[codey] attaching browser ...", file=sys.stderr)
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


def cmd_agent(args: argparse.Namespace) -> int:
    task = " ".join(args.task)
    project = Path(args.project).resolve()
    json_mode = getattr(args, "json", False) is True
    _safe_print(f"[codey] project: {project}", file=sys.stderr)
    if json_mode:
        from codey.headless_runner import (
            HeadlessRequest,
            emit_jsonl,
            run_headless,
        )
        from codey.local_store import DEFAULT_STATE_HOME

        state_home_arg = getattr(args, "state_home", None)
        state_home = (
            Path(state_home_arg).expanduser()
            if state_home_arg
            else DEFAULT_STATE_HOME
        )
        request = HeadlessRequest(
            project=project,
            task=task,
            provider_id=args.provider,
            max_turns=args.max_turns,
            intent=(
                "planning_readonly"
                if getattr(args, "readonly", False)
                else "project"
            ),
            state_home=state_home,
            port=args.port,
        )
        result = run_headless(
            request,
            emit_jsonl=lambda payload: emit_jsonl(payload, file=sys.stdout),
        )
        return result.exit_code

    from codey import provider_controls
    from codey.agent import run
    from codey.events import render_run_event
    from codey.providers import connect_provider

    project.mkdir(parents=True, exist_ok=True)
    provider_controls.begin_task_context(f"cli-agent:{args.provider}")
    provider = None
    try:
        provider = connect_provider(args.provider, port=args.port)
        def on_event(event) -> None:
            _safe_print(render_run_event(event), file=sys.stderr)

        result = run(
            provider,
            project,
            task,
            max_turns=args.max_turns,
            on_event=on_event,
        )
    finally:
        try:
            if provider is not None:
                provider.close()
        finally:
            provider_controls.end_task_context()
    _safe_print(result.summary, file=sys.stdout)
    return 0


def cmd_ghost(args: argparse.Namespace) -> int:
    from codey.ghost.inbox import GhostInboxStore
    from codey.ghost.store import GhostSignalStore
    from codey.local_store import DEFAULT_STATE_HOME

    state_home_arg = getattr(args, "state_home", "") or ""
    state_home = Path(state_home_arg).expanduser() if state_home_arg else DEFAULT_STATE_HOME
    store = GhostInboxStore(state_home)
    signal_store = GhostSignalStore(state_home)
    action = str(getattr(args, "ghost_cmd", "") or "").strip()
    if action == "list":
        candidates = [
            candidate.to_payload()
            for candidate in store.list_candidates(
                status=getattr(args, "status", "") or None,
                scope=getattr(args, "scope", "") or "",
                project=getattr(args, "project", "") or "",
                session_id=getattr(args, "session_id", "") or "",
            )
        ]
        _print_json({
            "schema_version": 1,
            "ok": True,
            "learning_enabled": store.learning_enabled(),
            "candidates": candidates,
            "warnings": list(store.last_warnings),
        })
        return 0
    if action == "export":
        payload = store.export_state()
        payload["signals"] = list(signal_store.read_all())
        payload["ok"] = True
        _print_json(payload)
        return 0
    if action == "reset":
        if not getattr(args, "yes", False):
            _print_error_json("reset requires --yes")
            return 2
        try:
            ok = store.reset_all(preserve_settings=True)
            signal_store.delete_all()
        except OSError as exc:
            _print_error_json(exc)
            return 1
        _print_json({"schema_version": 1, "ok": ok})
        return 0 if ok else 1
    if action == "delete-scope":
        if not getattr(args, "yes", False):
            _print_error_json("delete-scope requires --yes")
            return 2
        try:
            removed = store.delete_scope(
                args.scope_name,
                project=getattr(args, "project", "") or "",
                session_id=getattr(args, "session_id", "") or "",
            )
            signal_removed = signal_store.delete_scope(
                args.scope_name,
                project=getattr(args, "project", "") or "",
                session_id=getattr(args, "session_id", "") or "",
            )
        except ValueError as exc:
            _print_error_json(exc)
            return 2
        except (OSError, TypeError) as exc:
            _print_error_json(exc)
            return 1
        _print_json({
            "schema_version": 1,
            "ok": True,
            "removed_count": removed,
            "signal_removed_count": signal_removed,
        })
        return 0
    if action in {"enable", "disable"}:
        enabled = action == "enable"
        ok = store.set_learning_enabled(enabled)
        _print_json({
            "schema_version": 1,
            "ok": ok,
            "learning_enabled": store.learning_enabled(),
        })
        return 0 if ok else 1
    _safe_print("ghost subcommand required", file=sys.stderr)
    return 2


def _print_error_json(error: object) -> None:
    _print_json({
        "schema_version": 1,
        "ok": False,
        "error": str(error or "error")[:240],
    })


def _print_json(payload: dict[str, object]) -> None:
    _safe_print(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        file=sys.stdout,
    )


def _add_ghost_subcommands(sub) -> None:
    ghost_common = argparse.ArgumentParser(add_help=False)
    ghost_common.add_argument("--state-home", default="", help="local Codey state directory")

    sp_ghost_list = sub.add_parser("list", parents=[ghost_common], help="list inbox candidates")
    sp_ghost_list.add_argument("--status", default="", help="optional status filter")
    sp_ghost_list.add_argument("--scope", choices=("user", "project", "session"), default="", help="optional scope filter")
    sp_ghost_list.add_argument("--project", default="", help="project path for project scope filtering")
    sp_ghost_list.add_argument("--session-id", default="", help="session id for session scope filtering")
    sp_ghost_list.set_defaults(func=cmd_ghost)

    sp_ghost_export = sub.add_parser("export", parents=[ghost_common], help="export Ghost inbox/events/signals")
    sp_ghost_export.set_defaults(func=cmd_ghost)

    sp_ghost_reset = sub.add_parser("reset", parents=[ghost_common], help="delete Ghost inbox/events/signals")
    sp_ghost_reset.add_argument("--yes", action="store_true")
    sp_ghost_reset.set_defaults(func=cmd_ghost)

    sp_ghost_delete = sub.add_parser("delete-scope", parents=[ghost_common], help="delete one Ghost memory scope")
    sp_ghost_delete.add_argument("scope_name", choices=("user", "project", "session"))
    sp_ghost_delete.add_argument("--project", default="", help="required for project scope")
    sp_ghost_delete.add_argument("--session-id", default="", help="required for session scope")
    sp_ghost_delete.add_argument("--yes", action="store_true")
    sp_ghost_delete.set_defaults(func=cmd_ghost)

    sp_ghost_enable = sub.add_parser("enable", parents=[ghost_common], help="enable future Ghost learning ingest")
    sp_ghost_enable.set_defaults(func=cmd_ghost)

    sp_ghost_disable = sub.add_parser("disable", parents=[ghost_common], help="disable future Ghost learning ingest")
    sp_ghost_disable.set_defaults(func=cmd_ghost)


def _main_ghost(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="codey ghost")
    sub = ap.add_subparsers(dest="ghost_cmd", required=True)
    _add_ghost_subcommands(sub)
    args = ap.parse_args(argv)
    return args.func(args)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    # Default: if no subcommand given, launch the UI.
    if not argv or argv[0] not in {"ui", "chat", "agent", "ghost", "-h", "--help"}:
        argv = ["ui", *argv]

    if argv[0] == "ghost":
        return _main_ghost(argv[1:])

    from codey.providers import DEFAULT_PROVIDER_ID, provider_ids

    ap = argparse.ArgumentParser(prog="codey")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp_ui = sub.add_parser("ui", help="launch the native UI (default)")
    sp_ui.add_argument("--port", type=int, default=5173)
    sp_ui.set_defaults(func=cmd_ui)

    sp_chat = sub.add_parser("chat", help="single-shot prompt")
    sp_chat.add_argument("prompt", nargs="+")
    sp_chat.add_argument("--port", type=int, default=9222)
    sp_chat.add_argument("--provider", choices=provider_ids(), default=DEFAULT_PROVIDER_ID)
    sp_chat.add_argument("--timeout", type=float, default=300.0)
    sp_chat.set_defaults(func=cmd_chat)

    sp_agent = sub.add_parser("agent", help="CLI agent loop")
    sp_agent.add_argument("--project", required=True)
    from codey.agent import DEFAULT_MAX_TURNS
    sp_agent.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    sp_agent.add_argument("--port", type=int, default=9222)
    sp_agent.add_argument("--provider", choices=provider_ids(), default=DEFAULT_PROVIDER_ID)
    sp_agent.add_argument("--json", action="store_true", help="emit JSONL events on stdout")
    sp_agent.add_argument("--readonly", action="store_true", help="run a read-only planning task in JSONL mode")
    sp_agent.add_argument("--state-home", default="", help="local Codey state directory for JSONL mode")
    sp_agent.add_argument("task", nargs="+")
    sp_agent.set_defaults(func=cmd_agent)

    sub.add_parser("ghost", help="inspect and control Ghost memory inbox")

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
