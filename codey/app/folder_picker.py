"""Own the native folder dialog used by standalone browser clients."""
from __future__ import annotations

import threading
from concurrent.futures import Future
from pathlib import Path

_active = threading.Lock()


class FolderPickerBusy(RuntimeError):
    """A browser-owned native dialog is already open."""


def pick_folder(mode: str = "open", initial: str | None = None) -> str | None:
    if not _active.acquire(blocking=False):
        raise FolderPickerBusy
    result: Future[str | None] = Future()

    def choose() -> None:
        try:
            result.set_result(_choose_folder(mode, initial))
        except BaseException as exc:
            result.set_exception(exc)

    try:
        threading.Thread(target=choose, name="codey-folder-picker", daemon=True).start()
        return result.result()
    finally:
        _active.release()


def _choose_folder(mode: str, initial: str | None) -> str | None:
    import tkinter as tk
    from tkinter import filedialog

    initial_path = Path(initial).expanduser() if initial else Path.home()
    if not initial_path.is_dir():
        initial_path = Path.home()
    create = mode == "new"
    root = tk.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        selected: object = filedialog.askdirectory(
            parent=root,
            title="Create or Select Project Folder" if create else "Select Existing Project Folder",
            initialdir=str(initial_path),
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
