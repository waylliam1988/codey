"""Read-only Python definition lookup through a locally installed Pyright server."""
from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Sequence
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread
from typing import IO
from urllib.parse import urlsplit
from urllib.request import url2pathname

from codey.runtime.core import cancellation
from codey.toolchain.runtime import ToolOutcome, safe_join
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, parse_json_object, require_json_integer
from codey.workspace.paths import content_hash, ensure_not_symlink, path_hash

_MAX_PACKET = 2_000_000


def pyright_server() -> str | None:
    return shutil.which("pyright-langserver")


def _send(stream: IO[bytes], method: str, params: JsonObject | None = None, *, identity: int | None = None) -> None:
    message: JsonObject = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    if identity is not None:
        message["id"] = identity
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    stream.flush()


def _read_packets(stream: IO[bytes], output: SimpleQueue[JsonObject | Exception]) -> None:
    try:
        while True:
            length = 0
            while line := stream.readline(8192):
                if line in {b"\r\n", b"\n"}:
                    break
                if line.lower().startswith(b"content-length:"):
                    length = int(line.partition(b":")[2])
            if not 0 < length <= _MAX_PACKET:
                raise ValueError("language server closed the stream or sent an invalid message")
            body = stream.read(length)
            if len(body) != length:
                raise ValueError("incomplete language server message")
            packet = parse_json_object(body.decode("utf-8"))
            if "id" in packet:
                output.put(packet)
    except (OSError, ValueError) as exc:
        output.put(exc)


def _wait(output: SimpleQueue[JsonObject | Exception], identity: int, deadline: float) -> JsonValue:
    while (remaining := deadline - time.monotonic()) > 0:
        cancellation.check()
        try:
            packet = output.get(timeout=min(remaining, 0.1))
        except Empty:
            continue
        if isinstance(packet, Exception):
            raise ValueError(str(packet)) from packet
        if packet.get("id") == identity:
            if "error" in packet:
                raise ValueError("language server rejected the definition request")
            return packet.get("result")
    raise TimeoutError("language server definition call timed out")


def _lookup(argv: Sequence[str], root: Path, path: Path, text: str, line: int, character: int) -> JsonValue:
    process, tree = cancellation.start_process(argv, cwd=root, stdin=True)
    stdin, stdout, stderr = process.stdin, process.stdout, process.stderr
    assert stdin is not None and stdout is not None and stderr is not None
    output = SimpleQueue[JsonObject | Exception]()

    def drain_stderr() -> None:
        while stderr.read(8192):
            pass

    readers = (Thread(target=_read_packets, args=(stdout, output), daemon=True), Thread(target=drain_stderr, daemon=True))
    for reader in readers:
        reader.start()
    expires = time.monotonic() + 10
    uri = path.as_uri()
    try:
        _send(stdin, "initialize", {"processId": None, "rootUri": root.as_uri(), "capabilities": {}}, identity=1)
        _wait(output, 1, expires)
        _send(stdin, "initialized", {})
        _send(stdin, "textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": text}})
        _send(stdin, "textDocument/definition", {"textDocument": {"uri": uri},
              "position": {"line": line, "character": character}}, identity=2)
        result = _wait(output, 2, expires)
        _send(stdin, "textDocument/didClose", {"textDocument": {"uri": uri}})
        _send(stdin, "shutdown", identity=3)
        _wait(output, 3, expires)
        _send(stdin, "exit")
        return result
    finally:
        stdin.close()
        cancellation.terminate_process_tree(process, tree)
        if tree is not None:
            tree.close()
        for reader in readers:
            reader.join(timeout=0.5)
        stdout.close()
        stderr.close()


def _locations(root: Path, raw: JsonValue) -> list[JsonValue]:
    locations: list[JsonValue] = []
    for item in raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []:
        row = decode_json_object(item)
        uri = row.get("uri", row.get("targetUri"))
        if not isinstance(uri, str):
            continue
        parsed = urlsplit(uri)
        if parsed.scheme != "file" or parsed.netloc:
            continue
        uri_path = re.sub(r"^/([a-zA-Z])%3[aA]/", r"/\1:/", parsed.path)
        path = Path(url2pathname(uri_path)).resolve()
        if not path.is_relative_to(root):
            continue
        span = decode_json_object(row.get("range", row.get("targetSelectionRange")))
        start = decode_json_object(span["start"])
        locations.append({"path": path.relative_to(root).as_posix(),
            "line": require_json_integer(start["line"], "definition line") + 1,
            "column_utf16": require_json_integer(start["character"], "definition column") + 1})
    return locations


def semantic_definition(root: Path, rel: str, line: int, symbol: str) -> ToolOutcome:
    executable = pyright_server()
    if executable is None:
        return ToolOutcome.error("Python definition lookup requires pyright-langserver on PATH")
    root = root.resolve()
    path = ensure_not_symlink(safe_join(root, rel))
    if path.suffix != ".py" or path.stat().st_size > 1_000_000 or Path(executable).resolve().is_relative_to(root):
        return ToolOutcome.error("definition requires a bounded Python file and a server installed outside the project")
    text = path.read_bytes().decode("utf-8")
    version = content_hash(text)
    lines = text.splitlines()
    if not 1 <= line <= len(lines) or not symbol.isidentifier():
        return ToolOutcome.error("definition requires a valid line and identifier")
    occurrences = list(re.finditer(r"(?<!\w)" + re.escape(symbol) + r"(?!\w)", lines[line - 1]))
    if len(occurrences) != 1:
        return ToolOutcome.error("the identifier must occur exactly once on the requested line")
    character = len(lines[line - 1][:occurrences[0].start()].encode("utf-16-le")) // 2
    raw = _lookup((executable, "--stdio"), root, path, text, line - 1, character)
    if path_hash(path, preserve_newlines=True) != version:
        return ToolOutcome.error("the queried document changed while Pyright was running; reread it")
    result: JsonObject = {"path": rel, "file_version": "raw:" + version, "line": line,
                          "symbol": symbol, "definitions": _locations(root, raw)}
    return ToolOutcome(json.dumps(result, ensure_ascii=False), True, canonical=result)
