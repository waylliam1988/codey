"""Report source size for review; a line threshold never mandates a split."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

SOURCE_ROOTS = {"codey", "tools", "tests", ".github", "frontend"}
SOURCE_SUFFIXES = {".py", ".js", ".mjs", ".ts", ".css", ".html", ".yml"}


def inventory(root: Path, threshold: int = 1000) -> list[dict[str, object]]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, check=True,
    )
    rows = []
    for name in sorted(set(result.stdout.decode("utf8").split("\0")) - {""}):
        path = root / name
        if Path(name).parts[0] not in SOURCE_ROOTS or path.suffix not in SOURCE_SUFFIXES or not path.is_file():
            continue
        lines = len(path.read_text(encoding="utf8").splitlines())
        if lines >= threshold:
            rows.append({"path": name, "lines": lines, "surface": Path(name).parts[0]})
    return sorted(rows, key=lambda row: (-int(row["lines"]), str(row["path"])))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=int, default=1000)
    args = parser.parse_args()
    print(json.dumps(inventory(Path(__file__).resolve().parents[1], args.threshold), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
