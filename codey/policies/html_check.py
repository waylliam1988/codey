"""The fixed single-file HTML check exposed through the existing run tool."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Literal

_COMMAND = "codey-check-html"
_MAX_BYTES = 1024 * 1024
_COMPILE_CLASSIC = "new (require('node:vm').Script)(require('node:fs').readFileSync(0, 'utf8'))"
_CLASSIC_TYPES = frozenset({
    "application/ecmascript", "application/javascript", "application/x-ecmascript", "application/x-javascript",
    "text/ecmascript", "text/javascript", "text/javascript1.0", "text/javascript1.1", "text/javascript1.2",
    "text/javascript1.3", "text/javascript1.4", "text/javascript1.5", "text/jscript", "text/livescript",
    "text/x-ecmascript", "text/x-javascript",
})
_ScriptMode = Literal["classic", "module"]


def html_check_target(argv: Sequence[str]) -> str | None:
    if len(argv) != 2 or argv[0] != _COMMAND:
        return None
    raw = argv[1].replace("\\", "/")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or ":" in raw or path.suffix.lower() not in {".html", ".htm"}:
        return None
    return path.as_posix()


def html_check_command(path: str) -> str | None:
    target = html_check_target((_COMMAND, path))
    if target is None or any(char in target for char in "\r\n\0"):
        return None
    if os.name == "nt":
        arg = f'"{target}"' if target.startswith("'") or any(char.isspace() for char in target) else target
    else:
        arg = shlex.quote(target)
    return f"{_COMMAND} {arg}"


def html_check_process_args(path: Path) -> tuple[str, ...]:
    # Isolated mode and a trusted script path avoid importing checker code from the project.
    return sys.executable, "-I", "-S", "-X", "utf8", str(Path(__file__).resolve()), str(path)


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.has_element = False
        self.raw_tag = ""
        self.inline_scripts: list[tuple[int, _ScriptMode, str]] = []
        self._script_mode: _ScriptMode | None = None
        self._script_line = 0
        self._script_data: list[str] = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            raise ValueError(f"<{tag}> requires an explicit closing tag")
        super().handle_startendtag(tag, attrs)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.has_element = True
        if tag in {"script", "style"}:
            self.raw_tag = tag
        if tag == "script":
            # Browsers keep the first value of duplicate attributes.
            attributes = dict(reversed(attrs))
            kind = (attributes.get("type") or "").strip().lower()
            if "src" not in attributes:
                if kind == "module":
                    self._script_mode = "module"
                elif not kind or kind in _CLASSIC_TYPES:
                    self._script_mode = "classic"
            self._script_line = self.getpos()[0]

    def handle_data(self, data: str) -> None:
        if self._script_mode:
            self._script_data.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == self.raw_tag:
            self.raw_tag = ""
        if tag == "script" and self._script_mode:
            source = "".join(self._script_data)
            if source.strip():
                self.inline_scripts.append((self._script_line, self._script_mode, source))
            self._script_mode = None
            self._script_data.clear()


def _node_on_path() -> str | None:
    executable = "node.exe" if os.name == "nt" else "node"
    for entry in os.get_exec_path():
        directory = Path(entry)
        if directory.is_absolute():
            node = shutil.which(str(directory / executable))
            if node is not None:
                return node
    return None


def _check(path: Path) -> int:
    page = _Page()
    try:
        if not path.is_file():
            raise ValueError("expected an HTML file")
        with path.open("rb") as stream:
            data = stream.read(_MAX_BYTES + 1)
        if len(data) > _MAX_BYTES:
            raise ValueError(f"HTML exceeds the {_MAX_BYTES}-byte check limit")
        page.feed(data.decode("utf-8-sig"))
        page.close()
        if not page.has_element:
            raise ValueError("no HTML element found")
        if page.raw_tag:
            raise ValueError(f"<{page.raw_tag}> is missing its closing tag")
    except (OSError, ValueError) as exc:
        print(f"{path.name}: {exc}", file=sys.stderr)
        return 1

    if page.inline_scripts:
        node = _node_on_path()
        if node is None:
            print("node: command not found (required to check inline JavaScript)", file=sys.stderr)
            return 127
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("NODE_")}
        for line, mode, source in page.inline_scripts:
            flags = ["--check", "--input-type=module"] if mode == "module" else ["-e", _COMPILE_CLASSIC]
            result = subprocess.run(
                [node, *flags],
                input=source.encode("utf-8"), stderr=subprocess.PIPE, env=env, check=False,
            )
            if result.returncode:
                print(f"{path.name}:{line}: inline JavaScript syntax check failed", file=sys.stderr)
                print(result.stderr.decode("utf-8", errors="replace").rstrip(), file=sys.stderr)
                return 1
    print(f"{path.name} passed basic HTML checks and syntax checking of {len(page.inline_scripts)} inline scripts.")
    return 0


if __name__ == "__main__":
    sys.exit(_check(Path(sys.argv[1])))
