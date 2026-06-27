"""Codey entry point.

    python -m codey            launch the web UI (default)
    python -m codey ui         same as above, but with --port/--no-browser
    python -m codey chat ...   single-shot prompt, prints reply
    python -m codey agent ...  agent loop without UI (CLI mode)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def cmd_ui(args: argparse.Namespace) -> int:
    from codey.server import serve

    serve(host="127.0.0.1", port=args.port, open_in_browser=not args.no_browser)
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    from codey.browser import open_deepseek
    from codey.deepseek import chat as ds_chat

    prompt = " ".join(args.prompt)
    print("[codey] attaching Edge ...", file=sys.stderr)
    session = open_deepseek(port=args.port)
    try:
        reply = ds_chat(session.page, prompt, response_timeout=args.timeout)
    finally:
        session.pw.stop()
    print(reply)
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    from codey.agent import run
    from codey.browser import open_deepseek

    task = " ".join(args.task)
    project = Path(args.project).resolve()
    project.mkdir(parents=True, exist_ok=True)

    print(f"[codey] project: {project}", file=sys.stderr)
    session = open_deepseek(port=args.port)
    try:
        result = run(
            session.page,
            project,
            task,
            max_turns=args.max_turns,
            on_event=lambda m: print(m, file=sys.stderr),
        )
    finally:
        session.pw.stop()
    print(result.summary)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    # Default: if no subcommand given, launch the UI.
    if not argv or argv[0] not in {"ui", "chat", "agent", "-h", "--help"}:
        argv = ["ui", *argv]

    ap = argparse.ArgumentParser(prog="codey")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp_ui = sub.add_parser("ui", help="launch the web UI (default)")
    sp_ui.add_argument("--port", type=int, default=5173)
    sp_ui.add_argument("--no-browser", action="store_true")
    sp_ui.set_defaults(func=cmd_ui)

    sp_chat = sub.add_parser("chat", help="single-shot prompt")
    sp_chat.add_argument("prompt", nargs="+")
    sp_chat.add_argument("--port", type=int, default=9222)
    sp_chat.add_argument("--timeout", type=float, default=300.0)
    sp_chat.set_defaults(func=cmd_chat)

    sp_agent = sub.add_parser("agent", help="CLI agent loop")
    sp_agent.add_argument("--project", required=True)
    from codey.agent import DEFAULT_MAX_TURNS
    sp_agent.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    sp_agent.add_argument("--port", type=int, default=9222)
    sp_agent.add_argument("task", nargs="+")
    sp_agent.set_defaults(func=cmd_agent)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
