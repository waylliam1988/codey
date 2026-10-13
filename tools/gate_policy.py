"""Explicit release scope and source fingerprints for gate reports."""
from __future__ import annotations

import hashlib
from pathlib import Path

from codey.providers.catalog import API_CONNECTIONS


def scope_verdict(summary: dict, *, profile: str, required_cases: tuple[str, ...],
                  selected_cases: tuple[str, ...]) -> dict:
    if profile not in {"smoke", "release"}:
        raise ValueError("unsupported gate profile")
    missing = sorted(set(required_cases) - set(selected_cases))
    return {**summary, "profile": profile, "selected_cases": list(selected_cases),
            "required_release_cases": list(required_cases), "missing_release_cases": missing,
            "release_ready": profile == "release" and not missing and summary.get("ok") is True}


def source_fingerprints(root: Path, connection_id: str) -> dict[str, dict[str, str]]:
    """Hash shared production sources and the selected optional adapter only."""
    optional = {registration[1].rsplit(".", 1)[0].replace(".", "/")
                for registration in API_CONNECTIONS.values() if registration[1].count(".") > 2}
    optional.update(path.parent.relative_to(root).as_posix()
                    for path in (root / "codey/providers").glob("*/connection.py"))
    registration = API_CONNECTIONS.get(connection_id)
    selected = registration[1].rsplit(".", 1)[0].replace(".", "/") if registration else ""
    production = {}
    for path in sorted((root / "codey").rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".js", ".mjs", ".html", ".css", ".json"}:
            continue
        name = path.relative_to(root).as_posix()
        if any(name.startswith(prefix + "/") and prefix != selected for prefix in optional):
            continue
        production[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    harness = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in sorted((root / "tools").rglob("*.py"))}
    return {"production_hashes": production, "harness_hashes": harness}
