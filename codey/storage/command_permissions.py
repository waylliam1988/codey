"""User-owned, revocable exact-command grants, outside project configuration."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from codey.policies.command_line import ShellSpec, split_run_command
from codey.storage.atomic_io import write_bytes_atomic
from codey.storage.file_lock import with_file_lock


def command_identity(pending: dict[str, Any]) -> dict[str, str]:
    project = str(Path(str(pending.get("project") or ".")).resolve())
    cwd = str(Path(project, str(pending.get("cwd") or ".")).resolve())
    return {
        "project": os.path.normcase(project), "cwd": os.path.normcase(cwd),
        "command": str(pending.get("command") or ""),
        "executor": json.dumps(ShellSpec.from_payload(pending.get("shell")).to_payload(), sort_keys=True),
        "environment_sha256": hashlib.sha256(json.dumps(
            {key: os.environ.get(key, "") for key in ("PATH", "PATHEXT", "VIRTUAL_ENV", "CONDA_PREFIX", "COMSPEC")},
            sort_keys=True,
        ).encode()).hexdigest(),
    }


def project_rule_available(command: str) -> bool:
    """Offer exact project grants only for narrowly understood command families."""
    if any(token in command for token in ("\n", "\r", ";", "|", "&", ">", "<", "`", "$")):
        return False
    try:
        argv = split_run_command(command)
    except ValueError:
        return False
    if len(argv) < 2:
        return False
    executable = Path(argv[0]).name.lower().removesuffix(".exe")
    return ((executable == "git" and argv[1] in {"status", "diff", "log", "show"})
            or (executable in {"python", "py"} and len(argv) >= 3
                and argv[1:3] in (["-m", "unittest"], ["-m", "pytest"]))
            or (executable in {"npm", "pnpm", "cargo", "go"} and argv[1] in {"test", "check"}))


class CommandPermissionStore:
    def __init__(self, root: Path) -> None:
        self.path = root / "permissions" / "commands.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("rules"), list):
            raise ValueError("Invalid saved command permissions")
        rows = payload["rules"]
        required = {"id", "project", "cwd", "command", "executor", "environment_sha256", "session_id", "scope"}
        if any(not isinstance(row, dict) or set(row) != required
               or any(not isinstance(v, str) for v in row.values())
               or not row["id"] or row["scope"] not in {"session", "project"} for row in rows):
            raise ValueError("Invalid saved command permission")
        return [{str(key): str(value) for key, value in row.items()} for row in rows]

    def _write(self, rows: list[dict[str, str]]) -> None:
        write_bytes_atomic(self.path, json.dumps({"version": 1, "rules": rows}, ensure_ascii=False).encode("utf-8"))

    def list_rules(self, *, session_id: str, project: str = "", all_scopes: bool = False) -> list[dict[str, str]]:
        with with_file_lock(self.path):
            return [row for row in self._read()
                    if all_scopes or (row.get("scope") == "session" and row.get("session_id") == session_id)
                    or (project and row.get("scope") == "project"
                        and row.get("project") == os.path.normcase(str(Path(project).resolve())))]

    def matching(self, pending: dict[str, Any]) -> dict[str, str] | None:
        try:
            identity = command_identity(pending)
        except ValueError:
            return None
        return next((row for row in self.list_rules(session_id=str(pending.get("session_id") or ""),
                                              project=identity["project"])
                     if all(row.get(key) == value for key, value in identity.items())), None)

    def remember(self, pending: dict[str, Any], scope: str) -> None:
        if scope not in {"session", "project"}:
            raise ValueError("Invalid command permission scope")
        if scope == "project" and not project_rule_available(str(pending.get("command") or "")):
            raise ValueError("This command supports one-time or exact session approval only")
        row = {**command_identity(pending), "session_id": str(pending.get("session_id") or "") if scope == "session" else "",
               "scope": scope}
        with with_file_lock(self.path):
            rows = self._read()
            if not any(all(item.get(key) == value for key, value in row.items()) for item in rows):
                rows.append({"id": uuid.uuid4().hex, **row})
                self._write(rows)

    def revoke(self, rule_id: str) -> None:
        with with_file_lock(self.path):
            self._write([row for row in self._read() if row.get("id") != rule_id])
