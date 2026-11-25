"""Load executable frontend source for existing Node behavior regressions."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_cached_sources: dict[str, str] | None = None


def _sources() -> dict[str, str]:
    global _cached_sources
    if _cached_sources is not None:
        return _cached_sources
    result = subprocess.run(
        ["node", str(ROOT / "tools/frontend_source.mjs"), str(ROOT / "frontend/src")],
        check=True, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    decoded: object = json.loads(result.stdout)
    if not isinstance(decoded, dict):
        raise ValueError("frontend compiler output must be an object")
    fields: dict[object, object] = decoded
    sources: dict[str, str] = {}
    for key, value in fields.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("frontend compiler output must map module names to source text")
        sources[key] = value
    _cached_sources = sources
    return sources


def frontend_source_text(name: str) -> str:
    relative = name.removesuffix(".ts") + ".ts"
    return _sources()[relative]


def frontend_bundle(name: str) -> str:
    result = subprocess.run(
        ["node", str(ROOT / "tools/frontend_source.mjs"), "--bundle", str(ROOT / "frontend/src" / name)],
        check=True, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    return result.stdout
