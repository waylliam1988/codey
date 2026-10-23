"""Cache only successful state-independent release checks."""
from __future__ import annotations

import hashlib
import importlib.metadata
import os
import platform
import sys
from pathlib import Path
from typing import Any

from codey.control.protocol import fingerprint
from codey.storage.local_store import read_json_strict, write_json_atomic

VALIDATION_RULES = "compile-and-cli-v1"


def environment_digest() -> str:
    distributions = []
    for distribution in importlib.metadata.distributions():
        metadata = []
        for name in ("METADATA", "RECORD", "direct_url.json"):
            value = distribution.read_text(name) or ""
            metadata.append(hashlib.sha256(value.encode()).hexdigest())
        distributions.append([distribution.metadata["Name"], distribution.version, metadata])
    executable = Path(sys.executable).resolve()
    return fingerprint({"executable": str(executable), "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                        "version": sys.version, "implementation": sys.implementation.cache_tag,
                        "platform": platform.platform(), "prefix": sys.prefix, "base_prefix": sys.base_prefix,
                        "python_flags": str(sys.flags), "python_environment": {
                            name: value for name, value in os.environ.items() if name.startswith("PYTHON") or name == "VIRTUAL_ENV"
                        },
                        "distributions": sorted(distributions)})


def validation_key(digest: str, commands: list[list[str]]) -> str:
    return fingerprint({"runtime_digest": digest, "environment": environment_digest(),
                        "rules": VALIDATION_RULES, "commands": commands})


def cached_checks(home: Path, key: str, commands: list[list[str]]) -> list[dict[str, Any]] | None:
    try:
        value = read_json_strict(home / "validation-cache" / f"{key}.json")
    except (OSError, ValueError):
        return None
    if not value or value.get("key") != key or value.get("commands") != commands:
        return None
    rows = value.get("verification")
    if (not isinstance(rows, list) or len(rows) != len(commands)
            or any(not isinstance(row, dict) or row.get("argv") != command
                   or row.get("state") != "settled" or type(row.get("exit_code")) is not int or row.get("exit_code") != 0
                   for row, command in zip(rows, commands, strict=True))):
        return None
    return [dict(row, state="cached", cache_key=key) for row in rows]


def save_checks(home: Path, key: str, commands: list[list[str]], rows: list[dict[str, Any]]) -> None:
    write_json_atomic(home / "validation-cache" / f"{key}.json",
                      {"key": key, "commands": commands, "verification": rows}, mode=0o600)
