"""Own the native directory dialog and its result for standalone browsers."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from pathlib import Path
from uuid import uuid4

from codey.runtime.core import cancellation
from codey.runtime.core.process_io import ProcessOptions
from codey.types.json_value import parse_json_object

FOLDER_PICKER_PATH = "/api/pick_folder"
_RECENT_RESULTS = 32
logger = logging.getLogger(__name__)


def initial_directory(initial: str) -> str:
    path = Path(initial).expanduser() if initial else Path.home()
    return str(path if path.is_dir() else Path.home())


def folder_selection(path: str | None) -> dict[str, object]:
    if path is None:
        return {"ok": False, "cancelled": True}
    return {"ok": True, "path": path, "name": Path(path).name or path}


class FolderPicker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: str | None = None
        self._results: dict[str, dict[str, object]] = {}
        self._worker: threading.Thread | None = None
        self._closed = threading.Event()

    def start(self, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        mode = body.get("mode", "open")
        initial = body.get("initial", "")
        if mode not in ("open", "new") or not isinstance(mode, str) or not isinstance(initial, str):
            return 400, {"error": "invalid folder picker options"}
        with self._lock:
            if self._closed.is_set():
                return 503, {"error": "folder picker is closed"}
            if self._active is not None:
                return 409, {"error": "folder picker is busy"}
            identity = uuid4().hex
            worker = threading.Thread(target=self._run, args=(identity, mode, initial),
                                      name="codey-folder-picker", daemon=True)
            self._active = identity
            try:
                worker.start()
            except RuntimeError:
                self._active = None
                raise
            self._worker = worker
            return 202, {"id": identity}

    def status(self, identity: str) -> tuple[int, dict[str, object]]:
        with self._lock:
            if self._active == identity:
                return 202, {"id": identity}
            if (result := self._results.get(identity)) is not None:
                return 200, result
            return 404, {"error": "folder selection expired"}

    def _run(self, identity: str, mode: str, initial: str) -> None:
        result: dict[str, object] = {"error": "folder picker failed"}
        try:
            result = run_folder_dialog(mode, initial, self._closed)
        except Exception:
            logger.exception("Unexpected folder picker failure")
        finally:
            with self._lock:
                self._results[identity] = result
                if len(self._results) > _RECENT_RESULTS:
                    self._results.pop(next(iter(self._results)))
                self._active = None

    def close(self) -> None:
        with self._lock:
            self._closed.set()
            worker = self._worker
        if worker is not None:
            worker.join(timeout=2)


def run_folder_dialog(mode: str, initial: str, stop: threading.Event) -> dict[str, object]:
    try:
        with cancellation.scope(stop):
            return _run_dialog(mode, initial)
    except (OSError, ValueError, subprocess.SubprocessError, cancellation.TaskCancelled,
            cancellation.ProcessOutputReadError, cancellation.PipeDrainTimeout) as exc:
        return {"error": str(exc) or "folder picker failed"}


def _run_dialog(mode: str, initial: str) -> dict[str, object]:
    result = cancellation.run_process(
        [sys.executable, "-X", "utf8", "-m", "codey.app.folder_picker", mode, initial],
        cwd=Path.cwd(), timeout=600, capture_limit_bytes=128 * 1024,
        options=ProcessOptions(hide_console=True),
    )
    if result.returncode != 0 or result.stdout_truncated:
        raise ValueError(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "folder picker failed")
    return dict(parse_json_object(result.stdout))


def _choose_folder(mode: str, initial: str) -> str | None:
    import tkinter as tk
    from tkinter import filedialog

    create = mode == "new"
    root = tk.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        selected: object = filedialog.askdirectory(
            parent=root,
            title="Create or Select Project Folder" if create else "Select Existing Project Folder",
            initialdir=initial_directory(initial),
            mustexist=not create,
        )
    finally:
        root.destroy()
    if not selected:
        return None
    if not isinstance(selected, str):
        raise TypeError("Folder selection did not return a path")
    path = Path(selected).resolve()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return str(path)


if __name__ == "__main__":
    print(json.dumps(folder_selection(_choose_folder(sys.argv[1], sys.argv[2]))))
