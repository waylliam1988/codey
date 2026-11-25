"""Cache only successful state-independent release checks."""
from __future__ import annotations

import hashlib
import importlib.metadata
import math
import os
import platform
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from codey.control.protocol import fingerprint
from codey.storage.local_store import read_json_strict, write_json_atomic
from codey.utils.object_fields import object_fields

VALIDATION_RULES = "compile-and-cli-v1"


class Verification(TypedDict):
    argv: list[str]
    state: Literal["started", "settled", "timeout", "cached"]
    started_at: NotRequired[float]
    finished_at: NotRequired[float]
    elapsed_seconds: NotRequired[float]
    exit_code: NotRequired[int | None]
    cache_key: NotRequired[str]


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


def cached_checks(home: Path, key: str, commands: list[list[str]]) -> list[Verification] | None:
    try:
        value = read_json_strict(home / "validation-cache" / f"{key}.json")
    except (OSError, ValueError):
        return None
    if not value or value.get("key") != key or value.get("commands") != commands:
        return None
    raw = value.get("verification")
    if not isinstance(raw, list) or len(raw) != len(commands):
        return None
    rows: Sequence[object] = raw
    cached: list[Verification] = []
    for row, command in zip(rows, commands, strict=True):
        if (not object_fields(row) or row.get("argv") != command or row.get("state") != "settled"
                or type(row.get("exit_code")) is not int or row["exit_code"] != 0):
            return None
        check: Verification = {"argv": command, "state": "cached", "exit_code": 0, "cache_key": key}
        for field in ("started_at", "finished_at", "elapsed_seconds"):
            if field in row:
                timestamp = row[field]
                if (not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool)
                        or isinstance(timestamp, float) and not math.isfinite(timestamp)):
                    return None
                check[field] = timestamp
        cached.append(check)
    return cached


def save_checks(home: Path, key: str, commands: list[list[str]], rows: list[Verification]) -> None:
    write_json_atomic(home / "validation-cache" / f"{key}.json",
                      {"key": key, "commands": commands, "verification": rows}, mode=0o600)
