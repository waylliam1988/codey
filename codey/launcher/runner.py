"""Standalone stdlib-only coordinator, copied outside the replaceable release.

Never restores a database. Rollback boots the captured old runtime on the
current facts and only when its preflight schema contract still matches.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

TERMINAL = frozenset({"active", "rolled_back", "recovery_required", "aborted"})


def read(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Oversized coordinator state")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Coordinator state must be an object")
    return value


def write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def schema(home: Path) -> str:
    rows = []
    # AppContext owns both the durable and auxiliary SQLite stores. Compare all
    # known databases, excluding private backup/probe trees.
    for path in sorted(home.glob("*.sqlite3")):
        with contextlib.closing(sqlite3.connect(path)) as database:
            rows.append((path.name, database.execute("PRAGMA user_version").fetchone(),
                         database.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()))
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def request(home: Path, operation: str, instance: str = "", *, lifecycle: bool = True) -> dict[str, Any]:
    info = read(home / "service.json")
    if instance and instance != info["instance_id"]:
        raise RuntimeError("Service identity changed")
    url = urllib.parse.urlparse(info["url"])
    if url.scheme != "http" or url.hostname != "127.0.0.1" or url.username or url.password:
        raise ValueError("Coordinator discovery must name loopback")
    params: dict[str, Any] = {"expected_instance_id": info["instance_id"]} if lifecycle else {}
    message = urllib.request.Request(info["url"] + ("/api/lifecycle" if lifecycle else "/api/control"),
        json.dumps({"operation": operation, "params": params}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + read(home / "control-credentials.json")["operator"],
                 "X-Codey-Instance": info["instance_id"]})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(message, timeout=5) as response:
        value = json.loads(response.read(1024 * 1024))
    if value.get("ok") is not True:
        raise RuntimeError(str(value.get("error")))
    return dict(value["result"])


def mark(home: Path, journal: dict[str, Any], phase: str, **fields: Any) -> None:
    journal.update(phase=phase, **fields)
    journal["events"] = [*journal["events"], {"phase": phase, "time": time.time()}][-30:]
    write(home / "upgrades" / (journal["upgrade_id"] + ".json"), journal)


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


def launch(home: Path, journal: dict[str, Any], release: dict[str, Any], deadline: float) -> str:
    (home / "launcher").mkdir(exist_ok=True)
    argv = [journal["python"], "-m", "codey", "serve", "--state-home", str(home), "--verify-only",
            "--release-id", release["release_id"], "--port", str(journal.get("listen_port", 0))]
    for root in journal["allowed_roots"]:
        argv.extend(["--allow-project", root])
    with (home / "launcher" / (journal["upgrade_id"] + "-startup.log")).open("ab") as output:
        child = subprocess.Popen(argv, cwd=release["source"], stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=subprocess.STDOUT,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    while time.time() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f"Release exited during startup ({child.returncode})")
        try:
            health = request(home, "get_health", lifecycle=False)
            if health["release_id"] != release["release_id"] or health["instance_id"] == journal["old_instance_id"]:
                raise RuntimeError("Unexpected release instance")
            if health["status"] == "verifying":
                return str(health["instance_id"])
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    # Only the child created above can be terminated, never a discovered PID.
    child.terminate()
    child.wait(timeout=10)
    raise TimeoutError("Candidate startup exceeded its deadline")


def _cutover(home: Path, journal: dict[str, Any]) -> None:
    instance = journal["old_instance_id"]
    deadline = journal["deadline"]
    journal["listen_port"] = urllib.parse.urlparse(read(home / "service.json")["url"]).port
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
    mark(home, journal, "starting", attempts=journal["attempts"] + 1)
    candidate_instance = launch(home, journal, journal["candidate"], max(deadline, time.time() + 15))
    mark(home, journal, "verifying", candidate_instance_id=candidate_instance)
    if schema(home) != journal["schema_digest"]:
        raise RuntimeError("Candidate changed the live schema despite preflight")
    # Persist uncertainty BEFORE admission can open. Recovery never restores
    # pre-cutover files or assumes that a lost activate response meant failure.
    mark(home, journal, "opening", admission_may_have_opened=True)
    request(home, "activate", candidate_instance)
    write(home / "active-release.json", journal["candidate"])
    mark(home, journal, "active")


def _rollback(home: Path, journal: dict[str, Any], error: Exception) -> None:
    mark(home, journal, "rolling_back", error=str(error)[:2000])
    try:
        info = None
        with contextlib.suppress(OSError):
            info = read(home / "service.json")
        if info:
            if info["instance_id"] == journal["old_instance_id"]:
                request(home, "activate", info["instance_id"])
                mark(home, journal, "aborted")
                return
            if info["release_id"] != journal["candidate"]["release_id"]:
                raise RuntimeError("Unrelated service owns the state home")
            state = request(home, "drain", info["instance_id"])
            if not state["quiescent"]:
                raise RuntimeError("Candidate has work; preserve it for explicit recovery")
            request(home, "shutdown", info["instance_id"])
            wait_stopped(home, info["instance_id"], time.time() + 15)
        if schema(home) != journal.get("schema_digest"):
            raise RuntimeError("Current database is incompatible with the previous runtime")
        old_instance = launch(home, journal, journal["previous"], time.time() + 20)
        request(home, "activate", old_instance)
        write(home / "active-release.json", journal["previous"])
        mark(home, journal, "rolled_back", restored_instance_id=old_instance)
    except Exception as rollback_error:
        mark(home, journal, "recovery_required", recovery_error=str(rollback_error)[:2000])


def coordinate(home: Path, journal: dict[str, Any]) -> None:
    if journal["phase"] in TERMINAL:
        return
    if journal["phase"] != "accepted":
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
