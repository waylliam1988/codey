"""Syntax-check shipped JavaScript, including optional connector assets."""
from __future__ import annotations

import subprocess
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = sorted(path for directory in (root / "codey/web/assets", root / "codey/providers")
                   for path in directory.rglob("*") if path.suffix in {".js", ".mjs"})
    for path in paths:
        if path.parent == root / "codey/web/assets" and path.name.startswith("codey-"):
            subprocess.run(
                ["node", "--input-type=module", "--check"],
                input=path.read_text(encoding="utf-8"), text=True, encoding="utf-8", check=True,
            )
        else:
            subprocess.run(["node", "--check", str(path)], check=True)
    print(f"JavaScript syntax: {len(paths)} files checked")


if __name__ == "__main__":
    main()
