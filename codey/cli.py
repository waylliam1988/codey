"""Codey entry point.

    python -m codey            launch the native UI (default)
    python -m codey ui         same as above, with --port
    python -m codey chat ...   single-shot prompt, prints reply
    python -m codey agent ...  agent loop without UI (CLI mode)
"""

from __future__ import annotations

import argparse
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
    from codey.providers import connect_provider

    prompt = " ".join(args.prompt)
    _safe_print("[codey] attaching Edge ...", file=sys.stderr)
    provider = connect_provider(args.provider, port=args.port)
    try:
        reply = provider.send(prompt, timeout=args.timeout)
    finally:
        provider.close()
    _safe_print(reply)
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    from codey.agent import run
    from codey.providers import connect_provider

    task = " ".join(args.task)
    project = Path(args.project).resolve()
    project.mkdir(parents=True, exist_ok=True)

    _safe_print(f"[codey] project: {project}", file=sys.stderr)
    provider = connect_provider(args.provider, port=args.port)
    try:
        result = run(
            provider,
            project,
            task,
            max_turns=args.max_turns,
            on_event=lambda m: _safe_print(m, file=sys.stderr),
        )
    finally:
        provider.close()
    _safe_print(result.summary)
    return 0


def main(argv: list[str] | None = None) -> int:
    from codey.providers import DEFAULT_PROVIDER_ID, provider_ids

    argv = sys.argv[1:] if argv is None else argv

    # Default: if no subcommand given, launch the UI.
    if not argv or argv[0] not in {"ui", "chat", "agent", "-h", "--help"}:
        argv = ["ui", *argv]

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
    sp_agent.add_argument("task", nargs="+")
    sp_agent.set_defaults(func=cmd_agent)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
