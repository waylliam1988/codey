"""Capture real LSP requests and responses without adding a production logging path."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import IO
from unittest.mock import patch

from codey.toolchain import language_server as lsp
from codey.toolchain.semantic_definition import semantic_definition
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tools.completion_explanation_ab import append_json, write_json


class Options(argparse.Namespace):
    directory: Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    options = parser.parse_args(namespace=Options())
    directory = options.directory.resolve()
    project = directory / "project"
    project.mkdir(parents=True, exist_ok=False)
    (project / "broken.py").write_text('value: int = "wrong"\nprint(value)\n', encoding="utf-8")
    (project / "pyrightconfig.json").write_text('{"typeCheckingMode":"strict"}', encoding="utf-8")
    (project / "math.ts").write_text('export function total(value: number): number { return value * 2; }\n', encoding="utf-8")
    (project / "app.ts").write_text("import {total} from './math';\nconsole.log(total(3));\n", encoding="utf-8")
    (project / "tsconfig.json").write_text('{"compilerOptions":{"strict":true},"files":["math.ts","app.ts"]}', encoding="utf-8")
    original_send, original_receive = lsp._send, lsp._Connection.receive

    def send(stream: IO[bytes], message: JsonObject) -> None:
        append_json(directory / "wire.jsonl", {"direction": "request", "message": message})
        original_send(stream, message)

    def receive(connection: lsp._Connection, timeout: float) -> JsonObject | None:
        result = original_receive(connection, timeout)
        if result is not None:
            append_json(directory / "wire.jsonl", {"direction": "response", "message": result})
        return result

    results: list[JsonObject] = []
    with patch.object(lsp, "_send", send), patch.object(lsp._Connection, "receive", receive):
        for path, line, symbol, operation, new_name in (
            ("broken.py", 2, "value", "definition", ""),
            ("broken.py", 1, "value", "references", ""),
            ("broken.py", 1, "value", "hover", ""),
            ("broken.py", 1, "", "diagnostics", ""),
            ("math.ts", 1, "total", "references", ""),
            ("app.ts", 2, "total", "rename", "doubled"),
        ):
            outcome = semantic_definition(project, path, line, symbol, operation=operation, new_name=new_name)
            results.append({"path": path, "operation": operation, "ok": outcome.ok,
                            "text": outcome.model_text, "canonical": decode_json_object(outcome.canonical)})
    passed = all(row["ok"] for row in results)
    summary: JsonObject = {"passed": passed, "results": list[JsonValue](results),
        "installed_languages": [suffix for suffix in (".py", ".ts", ".js", ".go", ".rs")
                                if lsp.server_for(Path("example" + suffix)) is not None]}
    write_json(directory / "summary.json", summary)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
