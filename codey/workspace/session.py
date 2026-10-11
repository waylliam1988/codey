"""Resolve the shared tool workspace without granting access to application state."""

from pathlib import Path

from codey.storage.local_store import session_key


def session_workspace(state_home: str | Path, session_id: str) -> Path:
    base = Path(state_home).expanduser().resolve()
    root = base / "workspaces" / session_key(session_id)
    if root.resolve() != root or (base / "workspaces").is_symlink() or root.is_symlink():
        raise ValueError("session workspace redirects outside its managed location")
    root.mkdir(parents=True, exist_ok=True)
    return root
