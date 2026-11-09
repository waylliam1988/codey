"""Explicit installed-package probe, run with the target interpreter's -I flag."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import codey
from codey.control.client import ControlClient, ensure_service
from codey.launcher.runtime import read_runtime
from codey.research.search_factory import default_research_search_provider
from codey.research.web_sources import WebSourceProvider
from codey.storage.file_lock import acquire_lease


def stop_service(home: Path, client: ControlClient) -> None:
    discovery = home / "service.json"
    if not discovery.exists():
        return
    client.lifecycle("drain")
    deadline = time.monotonic() + 15
    while not client.lifecycle("status")["quiescent"]:
        assert time.monotonic() < deadline, "Service did not become quiescent"
        time.sleep(0.05)
    client.lifecycle("shutdown")
    deadline = time.monotonic() + 15
    while discovery.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not discovery.exists(), "Service did not remove its discovery registration"
    # Discovery is removed before ownership is released; await both.
    with acquire_lease(home / ".server.lock", timeout_seconds=5):
        pass


def probe(home: Path) -> None:
    root = Path(codey.__file__).resolve().parent.parent
    assert root.is_relative_to(Path(sys.prefix)), root
    assert not (root / "pyproject.toml").exists()
    assert not (root / "tools/publish_runtime.py").exists()
    assert read_runtime(root)["version"] == codey.__version__
    assert not (root / "codey/research/chat_web.py").exists()
    provider = default_research_search_provider()
    try:
        assert isinstance(provider.base_provider, WebSourceProvider)
        assert provider.base_provider._browser is None
    finally:
        provider.close()

    client = ControlClient(home, operator=True)
    try:
        client = ensure_service(home, port=0)
        first = client.call("get_health")
        assert first["status"] == "ready"
        assert ensure_service(home, port=0).call("get_health")["instance_id"] == first["instance_id"]
        init = root / "codey/__init__.py"
        init.write_bytes(init.read_bytes() + b"\n# installed update probe\n")
        second = ensure_service(home, port=0).call("get_health")
        assert second["status"] == "ready" and second["release_id"] != first["release_id"]
        assert second["instance_id"] != first["instance_id"]
        stop_service(home, client)
        third = ensure_service(home, port=0).call("get_health")
        assert third["status"] == "ready" and third["release_id"] == second["release_id"]
        assert third["instance_id"] != second["instance_id"]
        stop_service(home, client)
        print(json.dumps({"cold_start": "ready", "reconnect": "same instance",
                          "upgrade": "ready", "cold_restart": "ready"}))
    finally:
        failure = sys.exc_info()[1]
        try:
            stop_service(home, client)
        except Exception as cleanup_error:
            if failure is None:
                raise
            failure.add_note(f"Service cleanup also failed: {cleanup_error!r}")


if __name__ == "__main__":
    probe(Path(sys.argv[1]))
