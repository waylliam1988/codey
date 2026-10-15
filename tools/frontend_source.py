"""Load executable frontend source for existing Node behavior regressions."""
from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def _sources() -> dict[str, str]:
    result = subprocess.run(
        ["node", str(ROOT / "tools/frontend_source.mjs"), str(ROOT / "frontend/src")],
        check=True, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    return json.loads(result.stdout)


def frontend_source(name: str) -> str:
    key = name.removesuffix(".ts")
    if key == "legacy-runtime":
        key = "app_runtime"
    source = _sources()[key + ".ts"]
    # Standalone Node regressions execute one transpiled module without the
    # application entry. The fixture supplies the legacy object namespace.
    shim = (
        "var runtime = (typeof window !== 'undefined' ? window : globalThis);\n"
        "var decodeServerEvent = globalThis.decodeServerEvent || function(value) { return value; };\n"
    )
    return shim + source


def frontend_source_text(name: str) -> str:
    """Return compiler output; never rewrite TypeScript with regexes."""
    return frontend_source(name)

