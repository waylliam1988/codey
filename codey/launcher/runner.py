"""Standalone stdlib-only coordinator, copied outside the replaceable release.

Never restores a database. Rollback boots the captured old runtime on the
current facts and only when its preflight schema contract still matches.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import BinaryIO

if sys.platform == "win32":
    from subprocess import CREATE_NO_WINDOW
else:
    CREATE_NO_WINDOW = 0

TERMINAL = frozenset({"active", "rolled_back", "recovery_required", "aborted"})


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    row: Mapping[object, object] = value
    return {_text(key, f"{field} key"): item for key, item in row.items()}


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _items(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return value


def read(path: Path) -> dict[str, object]:
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Oversized coordinator state")
    value: object = json.loads(path.read_text(encoding="utf-8"))
    return _object(value, "Coordinator state")


def write(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    document: dict[str, object] = dict(value)
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(8):
        try:
            os.replace(temporary, path)
            break
        except PermissionError as exc:
            # Windows readers can briefly deny replacement of a journal.
            # A transient sharing failure must not roll back a healthy service.
            winerror: object = getattr(exc, "winerror", None)
            if winerror not in (5, 32, 33) or attempt == 7:
                raise
            time.sleep(0.01 * (1 << attempt))


def schema(home: Path) -> str:
    rows: list[tuple[str, tuple[object, ...] | None, list[tuple[str, str, str | None]]]] = []
    # AppContext owns both the durable and auxiliary SQLite stores. Compare all
    # known databases, excluding private backup/probe trees.
    for path in sorted(home.glob("*.sqlite3")):
        with contextlib.closing(sqlite3.connect(path)) as database:
            version: tuple[object, ...] | None = database.execute("PRAGMA user_version").fetchone()
            records: Sequence[tuple[object, object, object]] = database.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            ).fetchall()
            tables: list[tuple[str, str, str | None]] = []
            for raw_kind, raw_name, raw_sql in records:
                kind = _text(raw_kind, "schema object type")
                name = _text(raw_name, "schema object name")
                sql = None if raw_sql is None else _text(raw_sql, "schema object SQL")
                tables.append((kind, name, _compatible_schema_sql(path.name, kind, name, sql)))
            rows.append((path.name, version, tables))
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def _compatible_schema_sql(database: str, kind: str, name: str, sql: str | None) -> str | None:
    # Execution capture added optional metadata without changing any legacy
    # column or writer contract. Admit exactly those declarations; renamed,
    # constrained, retyped columns and all other schema changes remain visible.
    # This coordinator is copied as a standalone stdlib script at activation.
    if database == "execution_outputs.sqlite3" and kind == "table" and name == "execution_outputs" and sql:
        return re.sub(r", (?:stdout|stderr)_meta TEXT NOT NULL DEFAULT '\{\}'(?=,|\))", "", sql)
    return sql


def request(home: Path, operation: str, instance: str = "", *, lifecycle: bool = True) -> dict[str, object]:
    info = read(home / "service.json")
    instance_id = _text(info["instance_id"], "service instance ID")
    if instance and instance != instance_id:
        raise RuntimeError("Service identity changed")
    service_url = _text(info["url"], "service URL")
    url = urllib.parse.urlparse(service_url)
    if url.scheme != "http" or url.hostname != "127.0.0.1" or url.username or url.password:
        raise ValueError("Coordinator discovery must name loopback")
    params: dict[str, str] = {"expected_instance_id": instance_id} if lifecycle else {}
    operator = _text(read(home / "control-credentials.json")["operator"], "operator credential")
    payload: dict[str, object] = {"operation": operation, "params": params}
    message = urllib.request.Request(service_url + ("/api/lifecycle" if lifecycle else "/api/control"),
        json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + operator,
                 "X-Codey-Instance": instance_id})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    response: BinaryIO = opener.open(message, timeout=5)
    with response:
        raw: object = json.loads(response.read(1024 * 1024))
    value = _object(raw, "control response")
    if value.get("ok") is not True:
        raise RuntimeError(str(value.get("error")))
    return _object(value["result"], "control result")


def mark(home: Path, journal: dict[str, object], phase: str, **fields: object) -> None:
    journal.update(phase=phase, **fields)
    events = _items(journal["events"], "upgrade events")
    journal["events"] = [*events, {"phase": phase, "time": time.time()}][-30:]
    write(home / "upgrades" / (_text(journal["upgrade_id"], "upgrade ID") + ".json"), journal)


def wait_stopped(home: Path, instance: str, deadline: float) -> None:
    while time.time() < deadline:
        info = None
        with contextlib.suppress(OSError):
            info = read(home / "service.json")
        if info is None:
            return
        if info["instance_id"] != instance:
            raise RuntimeError("Another instance appeared while stopping")
        time.sleep(0.1)
    raise TimeoutError("Old instance did not relinquish ownership")


def launch(home: Path, journal: Mapping[str, object], release: Mapping[str, object], deadline: float) -> str:
    (home / "launcher").mkdir(exist_ok=True)
    release_id = _text(release["release_id"], "release ID")
    argv = [_text(journal["python"], "Python executable"), "-m", "codey", "serve", "--state-home", str(home),
            "--verify-only", "--release-id", release_id, "--port", str(journal.get("listen_port", 0))]
    for root in _items(journal["allowed_roots"], "allowed roots"):
        argv.extend(["--allow-project", _text(root, "allowed root")])
    identity = _text(journal["upgrade_id"], "upgrade ID")
    with (home / "launcher" / (identity + "-startup.log")).open("ab") as output:
        child = subprocess.Popen(argv, cwd=_text(release["source"], "release path"), stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=subprocess.STDOUT,
                                 creationflags=CREATE_NO_WINDOW)
    try:
        while time.time() < deadline:
            if child.poll() is not None:
                raise RuntimeError(f"Release exited during startup ({child.returncode})")
            try:
                health = request(home, "get_health", lifecycle=False)
                if health["release_id"] != release_id or health["instance_id"] == journal["old_instance_id"]:
                    raise RuntimeError("Unexpected release instance")
                if health["status"] == "verifying":
                    return _text(health["instance_id"], "service instance ID")
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        raise TimeoutError("Candidate startup exceeded its deadline")
    except BaseException:
        # Only the child created above may be terminated.
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
        raise


def _cutover(home: Path, journal: dict[str, object]) -> None:
    instance = _text(journal["old_instance_id"], "old instance ID")
    deadline = journal["deadline"]
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
        raise ValueError("upgrade deadline must be numeric")
    service_url = _text(read(home / "service.json")["url"], "service URL")
    journal["listen_port"] = urllib.parse.urlparse(service_url).port
    mark(home, journal, "draining")
    request(home, "drain", instance)
    while time.time() < deadline:
        state = request(home, "status", instance)
        if state["quiescent"]:
            break
        time.sleep(0.1)
    else:
        request(home, "activate", instance)
        mark(home, journal, "aborted", error="Drain deadline exceeded; original instance resumed")
        return
    journal["schema_digest"] = schema(home)
    mark(home, journal, "quiescent")
    request(home, "shutdown", instance)
    wait_stopped(home, instance, max(deadline, time.time() + 10))
    attempts = journal["attempts"]
    if type(attempts) is not int:
        raise ValueError("upgrade attempts must be an integer")
    mark(home, journal, "starting", attempts=attempts + 1)
    candidate = _object(journal["candidate"], "candidate release")
    candidate_instance = launch(home, journal, candidate, max(deadline, time.time() + 15))
    mark(home, journal, "verifying", candidate_instance_id=candidate_instance)
    if schema(home) != journal["schema_digest"]:
        raise RuntimeError("Candidate changed the live schema despite preflight")
    # Persist uncertainty BEFORE admission can open. Recovery never restores
    # pre-cutover files or assumes that a lost activate response meant failure.
    mark(home, journal, "opening", admission_may_have_opened=True)
    request(home, "activate", candidate_instance)
    write(home / "active-release.json", candidate)
    mark(home, journal, "active")


def _rollback(home: Path, journal: dict[str, object], error: Exception) -> None:
    mark(home, journal, "rolling_back", error=str(error)[:2000])
    try:
        info = None
        with contextlib.suppress(OSError):
            info = read(home / "service.json")
        if info:
            instance = _text(info["instance_id"], "service instance ID")
            if instance == journal["old_instance_id"]:
                request(home, "activate", instance)
                mark(home, journal, "aborted")
                return
            if info["release_id"] != _object(journal["candidate"], "candidate release")["release_id"]:
                raise RuntimeError("Unrelated service owns the state home")
            state = request(home, "drain", instance)
            if not state["quiescent"]:
                raise RuntimeError("Candidate has work; preserve it for explicit recovery")
            request(home, "shutdown", instance)
            wait_stopped(home, instance, time.time() + 15)
        if schema(home) != journal.get("schema_digest"):
            raise RuntimeError("Current database is incompatible with the previous runtime")
        previous = _object(journal["previous"], "previous release")
        old_instance = launch(home, journal, previous, time.time() + 20)
        request(home, "activate", old_instance)
        write(home / "active-release.json", previous)
        mark(home, journal, "rolled_back", restored_instance_id=old_instance)
    except Exception as rollback_error:
        mark(home, journal, "recovery_required", recovery_error=str(rollback_error)[:2000])


def coordinate(home: Path, journal: dict[str, object]) -> None:
    phase = _text(journal["phase"], "upgrade phase")
    if phase in TERMINAL:
        return
    if phase != "accepted":
        # A coordinator crash is reconciled conservatively. Never blindly
        # repeat a cutover or admit work merely because a process restarted.
        _rollback(home, journal, RuntimeError("Coordinator resumed an interrupted transition"))
        return
    try:
        _cutover(home, journal)
    except Exception as exc:
        _rollback(home, journal, exc)


def main() -> int:
    home = Path(sys.argv[1]).resolve()
    identity = sys.argv[2]
    if len(identity) != 40 or not identity.startswith("upgrade_") or any(char not in "0123456789abcdef" for char in identity[8:]):
        raise ValueError("Invalid upgrade journal identity")
    # Kernel-owned nonblocking lock survives crashes without stale-PID logic.
    with (home / "launcher" / "coordinator.lock").open("a+b") as lock:
        lock.seek(0)
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        coordinate(home, read(home / "upgrades" / (identity + ".json")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
