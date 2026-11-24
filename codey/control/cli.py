"""Lazy CLI registration for shared service and transport adapters."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from codey.control.protocol import COMMANDS, ControlError
from codey.storage.local_store import DEFAULT_STATE_HOME
from codey.types.json_value import require_json_boolean, require_json_integer, require_json_string, require_json_strings
from codey.utils.object_fields import object_fields

if TYPE_CHECKING:
    from codey.control.client import ControlClient

ALIASES = {"create": "create_conversation", "list": "list_conversations", "read": "read_conversation",
           "send": "send_message", "wait": "wait_run", "result": "get_run_result", "evidence": "read_run_evidence",
           "review": "record_review", "stop": "stop_run", "pause": "pause_control", "grant": "grant_control",
           "health": "get_health", "prepare": "prepare_release", "upgrade": "activate_release", "upgrade-status": "get_upgrade",
           "answer": "answer_question", "owner": "get_control", "resume-scheduler": "resume_scheduler"}


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-home", default=str(DEFAULT_STATE_HOME))
    common.add_argument("--operator", action="store_true", help="use the explicitly user-owned control capability")
    serve = subparsers.add_parser("serve", parents=[common], help="run the shared service without a desktop")
    serve.add_argument("--port", type=int, default=0)
    no_project_paths: list[str] = []
    serve.add_argument("--allow-project", action="append", default=no_project_paths)
    serve.add_argument("--verify-only", action="store_true", help=argparse.SUPPRESS)
    serve.add_argument("--release-id", default="", help=argparse.SUPPRESS)
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
                parser.add_argument(flag, dest=key, type=_parse_json_argument, default=argparse.SUPPRESS)
            elif kind == "boolean":
                parser.add_argument(flag, dest=key, action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS)
            else:
                parser.add_argument(flag, dest=key, type=int if kind == "integer" else float if kind == "number" else str,
                                    default=argparse.SUPPRESS)
        parser.set_defaults(func=run_control, control_operation=operation)


def _parse_json_argument(text: str) -> object:
    value: object = json.loads(text)
    return value


def _argument(args: argparse.Namespace, name: str) -> object:
    value: object = getattr(args, name)
    return value


def run_serve(args: argparse.Namespace) -> int:
    from codey.app.service import LocalService

    project_paths = require_json_strings(_argument(args, "allow_project"), "allow_project")
    roots = tuple(Path(root).resolve() for root in project_paths) or (Path.cwd(),)
    LocalService(
        Path(require_json_string(_argument(args, "state_home"), "state_home")),
        port=require_json_integer(_argument(args, "port"), "port"),
        allowed_roots=roots,
        verify_only=require_json_boolean(_argument(args, "verify_only"), "verify_only"),
        release_id=require_json_string(_argument(args, "release_id"), "release_id"),
    ).run()
    return 0


def run_transport(args: argparse.Namespace) -> int:
    client = _client(args)
    if _argument(args, "cmd") == "rpc":
        from codey.control.rpc import run

        return run(client)
    from codey.control.mcp import run as run_mcp

    return run_mcp(client)


def run_control(args: argparse.Namespace) -> int:
    try:
        params = _parse_json_argument(require_json_string(_argument(args, "params"), "params"))
        if not object_fields(params):
            raise ControlError("INVALID_ARGUMENT", "--params must contain an object")
        operation = require_json_string(_argument(args, "control_operation"), "control_operation")
        command = COMMANDS[operation]
        for key in command.fields:
            if hasattr(args, key):
                params[key] = _argument(args, key)
        text_file = require_json_string(_argument(args, "text_file"), "text_file")
        if text_file:
            params["text"] = Path(text_file).read_text(encoding="utf-8")
        if not command.readonly and "request_id" in command.fields and "request_id" not in params:
            params["request_id"] = uuid.uuid4().hex
        client = _client(args)
        if "control_generation" in command.fields and "control_generation" not in params:
            conversation_id = params.get("conversation_id")
            if not isinstance(conversation_id, str) or not conversation_id.strip():
                raise ControlError("INVALID_ARGUMENT", "conversation_id is required to look up the control generation")
            snapshot: dict[str, object] = client.call("get_control", {"conversation_id": conversation_id})
            params["control_generation"] = snapshot["control_generation"]
        if operation == "activate_release":
            info: dict[str, object] = client.discover()
            params.setdefault("expected_instance_id", info["instance_id"])
        result: dict[str, object] = client.call(operation, params)
        success: dict[str, object] = {"ok": True, "result": result}
        print(json.dumps(success, ensure_ascii=False, allow_nan=False))
        return 0
    except (ControlError, ValueError, OSError) as exc:
        error: dict[str, object] = {"code": "INVALID_ARGUMENT", "message": str(exc)}
        if isinstance(exc, ControlError):
            error = exc.payload()
        failure: dict[str, object] = {"ok": False, "error": error}
        print(json.dumps(failure, ensure_ascii=False))
        return 1


def _client(args: argparse.Namespace) -> ControlClient:
    from codey.control.client import ControlClient

    state_home = require_json_string(_argument(args, "state_home"), "state_home")
    operator = require_json_boolean(_argument(args, "operator"), "operator")
    return ControlClient(Path(state_home), operator=operator)
