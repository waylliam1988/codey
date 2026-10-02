from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

ERROR_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+): error: (?P<message>.*?)(?: \[(?P<code>[^]]+)\])?$"
)
NUMBER_RE = re.compile(r"\b\d+\b")


def _root_pattern(code: str, message: str) -> str:
    normalized = NUMBER_RE.sub("N", message)
    if code == "call-overload" and '"int"' in message:
        return "numeric coercion: int(object)"
    if code in {"arg-type", "call-overload"} and '"float"' in message:
        return "numeric coercion: float(object)"
    if code == "union-attr" and 'Item "None"' in message:
        return "nullable member access without a guard"
    if code == "attr-defined" and '"object" has no attribute' in message:
        return "object-typed value used as a structured object"
    if code == "index" and "object" in message:
        return "object-typed value indexed or assigned"
    if code == "arg-type" and "TaskState" in message:
        return "TaskState Protocol implementation mismatch"
    if code == "misc" and "Cannot infer type of lambda" in message:
        return "untyped lambda boundary"
    return f"{code}: {normalized}"


def analyze(path: Path) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        match = ERROR_RE.match(raw_line)
        if match is None:
            continue
        row = match.groupdict()
        code = row["code"] or "uncoded"
        message = row["message"]
        rows.append(
            {
                "file": row["file"],
                "line": int(row["line"]),
                "code": code,
                "message": message,
                "root_pattern": _root_pattern(code, message),
            }
        )

    by_code = collections.Counter(str(row["code"]) for row in rows)
    by_file = collections.Counter(str(row["file"]) for row in rows)
    by_pattern = collections.Counter(str(row["root_pattern"]) for row in rows)
    by_message = collections.Counter(
        (str(row["code"]), NUMBER_RE.sub("N", str(row["message"]))) for row in rows
    )
    return {
        "source": str(path),
        "error_count": len(rows),
        "file_count": len(by_file),
        "rows": rows,
        "by_code": dict(by_code.most_common()),
        "by_file": dict(by_file.most_common()),
        "by_root_pattern": dict(by_pattern.most_common()),
        "unique_normalized_messages": len(by_message),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify a mypy baseline output file.")
    parser.add_argument("baseline", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(args.baseline), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
