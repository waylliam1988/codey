"""One bounded stdio LSP client shared by installed language servers."""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread
from typing import IO
from urllib.parse import urlsplit
from urllib.request import url2pathname

from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, parse_json_object


@dataclass(frozen=True)
class LanguageServer:
    command: str
    language: str
    arguments: tuple[str, ...] = ("--stdio",)


_SERVERS = {
    ".py": ("pyright-langserver", "python", ("--stdio",)),
    ".js": ("typescript-language-server", "javascript", ("--stdio",)),
    ".jsx": ("typescript-language-server", "javascriptreact", ("--stdio",)),
    ".mjs": ("typescript-language-server", "javascript", ("--stdio",)),
    ".cjs": ("typescript-language-server", "javascript", ("--stdio",)),
    ".ts": ("typescript-language-server", "typescript", ("--stdio",)),
    ".tsx": ("typescript-language-server", "typescriptreact", ("--stdio",)),
    ".go": ("gopls", "go", ("serve",)),
    ".rs": ("rust-analyzer", "rust", ()),
}


def server_for(path: Path) -> LanguageServer | None:
    selected = _SERVERS.get(path.suffix.lower())
    if selected is None:
        return None
    name, language, arguments = selected
    command = shutil.which(name)
    return LanguageServer(command, language, arguments) if command else None


def available_server() -> bool:
    return any(shutil.which(name) for name, _, _ in _SERVERS.values())


def document_path(uri: str) -> Path | None:
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parsed.netloc:
        return None
    return Path(url2pathname(re.sub(r"^/([a-zA-Z])%3[aA]/", r"/\1:/", parsed.path))).resolve()


def _send(stream: IO[bytes], message: JsonObject) -> None:
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    stream.flush()


def _packets(stream: IO[bytes], queue: SimpleQueue[JsonObject | Exception]) -> None:
    try:
        while True:
            size = 0
            while line := stream.readline(8192):
                if line in {b"\r\n", b"\n"}:
                    break
                if line.lower().startswith(b"content-length:"):
                    size = int(line.partition(b":")[2])
            if not 0 < size <= 2_000_000:
                raise ValueError("language server closed or exceeded its packet limit")
            data = stream.read(size)
            if len(data) != size:
                raise ValueError("incomplete language server packet")
            queue.put(parse_json_object(data.decode("utf-8")))
    except (OSError, ValueError) as exc:
        queue.put(exc)


class _Connection:
    def __init__(self, stream: IO[bytes], queue: SimpleQueue[JsonObject | Exception], uri: str) -> None:
        self.stream, self.queue, self.uri = stream, queue, uri
        self.diagnostics: JsonValue = None
        self.next_id = 0
        self.deadline = time.monotonic() + 25

    def notify(self, method: str, params: JsonObject) -> None:
        _send(self.stream, {"jsonrpc": "2.0", "method": method, "params": params})

    def receive(self, timeout: float) -> JsonObject | None:
        cancellation.check()
        try:
            row = self.queue.get(timeout=max(0, min(timeout, self.deadline - time.monotonic())))
        except Empty:
            return None
        if isinstance(row, Exception):
            raise ValueError(str(row)) from row
        method = row.get("method")
        if isinstance(method, str) and "id" in row:
            params = decode_json_object(row.get("params", {}))
            items = params.get("items", [])
            result: JsonValue = ([None for _ in items] if isinstance(items, list) else []) if method == "workspace/configuration" else None
            if method == "workspace/applyEdit":
                result = {"applied": False, "failureReason": "read-only language client"}
            _send(self.stream, {"jsonrpc": "2.0", "id": row["id"], "result": result})
        elif method == "textDocument/publishDiagnostics":
            params = decode_json_object(row.get("params"))
            uri = params.get("uri")
            if isinstance(uri, str) and document_path(uri) == document_path(self.uri):
                self.diagnostics = params.get("diagnostics", [])
        return row

    def request(self, method: str, params: JsonObject | None = None) -> JsonValue:
        self.next_id += 1
        identity = self.next_id
        row: JsonObject = {"jsonrpc": "2.0", "id": identity, "method": method}
        if params is not None:
            row["params"] = params
        _send(self.stream, row)
        while time.monotonic() < self.deadline:
            packet = self.receive(0.1)
            if packet is not None and packet.get("id") == identity and "method" not in packet:
                if "error" in packet:
                    raise ValueError("language server rejected request: " + str(packet["error"]))
                return packet.get("result")
        self.notify("$/cancelRequest", {"id": identity})
        raise TimeoutError("language server request timed out")


def lookup(server: LanguageServer, root: Path, path: Path, text: str, line: int, character: int,
           *, operation: str, new_name: str = "") -> JsonValue:
    process, tree = cancellation.start_process((server.command, *server.arguments), cwd=root, stdin=True)
    stdin, stdout, stderr = process.stdin, process.stdout, process.stderr
    assert stdin is not None and stdout is not None and stderr is not None
    queue: SimpleQueue[JsonObject | Exception] = SimpleQueue()

    def drain() -> None:
        while stderr.read(8192):
            pass

    readers = (Thread(target=_packets, args=(stdout, queue), daemon=True), Thread(target=drain, daemon=True))
    for reader in readers:
        reader.start()
    wire = _Connection(stdin, queue, path.as_uri())
    try:
        initialization: JsonObject = {}
        if server.language in {"javascript", "javascriptreact", "typescript", "typescriptreact"}:
            initialization = {"tsserver": {"useSyntaxServer": "never"}, "disableAutomaticTypingAcquisition": True}
        initialized = decode_json_object(wire.request("initialize", {"processId": None, "rootUri": root.as_uri(),
            "initializationOptions": initialization,
            "workspaceFolders": [{"uri": root.as_uri(), "name": root.name}],
            "capabilities": {"general": {"positionEncodings": ["utf-16"]}, "textDocument": {
                "hover": {"contentFormat": ["plaintext"]}, "publishDiagnostics": {"versionSupport": True}}}}))
        wire.notify("initialized", {})
        wire.notify("textDocument/didOpen", {"textDocument": {"uri": path.as_uri(), "languageId": server.language,
            "version": 1, "text": text}})
        params: JsonObject = {"textDocument": {"uri": path.as_uri()}}
        if operation not in {"diagnostics", "documentSymbol"}:
            params["position"] = {"line": line, "character": character}
        if operation == "references":
            params["context"] = {"includeDeclaration": True}
        if operation == "rename":
            params["newName"] = new_name
        if operation == "diagnostics":
            capabilities = decode_json_object(initialized.get("capabilities", {}))
            if capabilities.get("diagnosticProvider"):
                result = wire.request("textDocument/diagnostic", params)
            else:
                wire.request("textDocument/documentSymbol", params)
                until = min(wire.deadline, time.monotonic() + 3)
                while time.monotonic() < until:
                    wire.receive(0.1)
                result = {"kind": "observed", "items": wire.diagnostics, "complete": False}
        else:
            result = wire.request("textDocument/" + operation, params)
        wire.notify("textDocument/didClose", {"textDocument": {"uri": path.as_uri()}})
        wire.request("shutdown")
        wire.notify("exit", {})
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
