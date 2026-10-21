"""Lazy CLI registration for shared service and transport adapters."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any

from codey.control.protocol import COMMANDS, ControlError
from codey.storage.local_store import DEFAULT_STATE_HOME

ALIASES = {"create": "create_conversation", "list": "list_conversations", "read": "read_conversation",
           "send": "send_message", "wait": "wait_run", "result": "get_run_result", "evidence": "read_run_evidence",
           "review": "record_review", "stop": "stop_run", "pause": "pause_control", "grant": "grant_control",
           "health": "get_health", "prepare": "prepare_release", "upgrade": "activate_release", "upgrade-status": "get_upgrade",
           "answer": "answer_question", "owner": "get_control"}


def register(subparsers: Any) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-home", default=str(DEFAULT_STATE_HOME))
    common.add_argument("--operator", action="store_true", help="use the explicitly user-owned control capability")
    serve = subparsers.add_parser("serve", parents=[common], help="run the shared service without a desktop")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--allow-project", action="append", default=[])
    serve.add_argument("--verify-only", action="store_true", help=argparse.SUPPRESS)
    serve.add_argument("--release-id", default="source", help=argparse.SUPPRESS)
    serve.set_defaults(func=run_serve)
    for name in ("rpc", "mcp"):
        parser = subparsers.add_parser(name, parents=[common], help=f"serve the {name.upper()} stdio control adapter")
        parser.set_defaults(func=run_transport)
    control = subparsers.add_parser("control", help="control the shared Codey service")
    actions = control.add_subparsers(dest="control_action", required=True)
    for alias, operation in ALIASES.items():
        parser = actions.add_parser(alias, parents=[common], help=COMMANDS[operation].description)
        parser.add_argument("--json", action="store_true", help="print a structured result")
        parser.add_argument("--params", default="{}", help="JSON arguments from the common command schema")
        parser.add_argument("--text-file", default="")
        for key, kind in COMMANDS[operation].fields.items():
            flag = {"conversation_id": "--session", "run_id": "--run", "candidate_release": "--candidate", "timeout_seconds": "--timeout"}.get(key, "--" + key.replace("_", "-"))
            if kind in {"object", "array"}:
                parser.add_argument(flag, dest=key, type=json.loads, default=argparse.SUPPRESS)
            elif kind == "boolean":
                parser.add_argument(flag, dest=key, action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS)
            else:
                parser.add_argument(flag, dest=key, type=int if kind == "integer" else float if kind == "number" else str,
                                    default=argparse.SUPPRESS)
        parser.set_defaults(func=run_control, control_operation=operation)


def run_serve(args: argparse.Namespace) -> int:
    from codey.app.service import LocalService

    roots = tuple(Path(root).resolve() for root in args.allow_project) or (Path.cwd(),)
    LocalService(Path(args.state_home), port=args.port, allowed_roots=roots,
                 verify_only=args.verify_only, release_id=args.release_id).run()
    return 0


def run_transport(args: argparse.Namespace) -> int:
    from codey.control.client import ControlClient

    client = ControlClient(Path(args.state_home), operator=args.operator)
    if args.cmd == "rpc":
        from codey.control.rpc import run

        return run(client)
    from codey.control.mcp import run as run_mcp

    return run_mcp(client)


def run_control(args: argparse.Namespace) -> int:
    from codey.control.client import ControlClient

    try:
        params = json.loads(args.params)
        if not isinstance(params, dict):
            raise ControlError("INVALID_ARGUMENT", "--params must contain an object")
        operation = args.control_operation
        for key in COMMANDS[operation].fields:
            if hasattr(args, key):
                params[key] = getattr(args, key)
        if args.text_file:
            params["text"] = Path(args.text_file).read_text(encoding="utf-8")
        if not COMMANDS[operation].readonly and "request_id" not in params:
            params["request_id"] = uuid.uuid4().hex
        client = ControlClient(Path(args.state_home), operator=args.operator)
        if "control_generation" in COMMANDS[operation].fields and "control_generation" not in params:
            params["control_generation"] = client.call("get_control", {"conversation_id": params["conversation_id"]})["control_generation"]
        if operation == "activate_release":
            params.setdefault("expected_instance_id", client.discover()["instance_id"])
        result = client.call(operation, params)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, allow_nan=False))
        return 0
    except (ControlError, ValueError, OSError) as exc:
        error = exc.payload() if isinstance(exc, ControlError) else {"code": "INVALID_ARGUMENT", "message": str(exc)}
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=False))
        return 1
