"""Syntax-check shipped JavaScript, including optional connector assets."""
from __future__ import annotations

import subprocess
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = sorted(path for directory in (root / "codey/web/assets", root / "codey/providers")
                   for path in directory.rglob("*") if path.suffix in {".js", ".mjs"})
    for path in paths:
        subprocess.run(["node", "--check", str(path)], check=True)
    print(f"JavaScript syntax: {len(paths)} files checked")


if __name__ == "__main__":
    main()
