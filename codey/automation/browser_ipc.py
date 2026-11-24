"""Bounded binary framing shared by the task-browser parent and child."""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Protocol

from codey.types.json_value import JsonObject, parse_json_object

MAX_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_BLOB_BYTES = 8 * 1024 * 1024
PROTOCOL_VERSION = 1
_HEADER = struct.Struct("!III")


class ReadStream(Protocol):
    def read(self, size: int = -1, /) -> bytes | None: ...


class WriteStream(Protocol):
    def write(self, data: bytes | memoryview, /) -> int | None: ...
    def flush(self) -> None: ...


@dataclass(frozen=True)
class BrowserPacket:
    message: JsonObject
    blob: bytes = b""


def _read_exact(stream: ReadStream, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        data = stream.read(remaining)
        if not data:
            raise EOFError("browser worker pipe closed mid-frame")
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def read_packet(stream: ReadStream) -> BrowserPacket:
    # The fixed !III layout returns three unsigned integers.
    fields: tuple[int, ...] = _HEADER.unpack(_read_exact(stream, _HEADER.size))
    version, size, blob_size = fields
    if version != PROTOCOL_VERSION or not 0 < size <= MAX_MESSAGE_BYTES or blob_size > MAX_BLOB_BYTES:
        raise ValueError("invalid browser protocol version or frame length")
    message = parse_json_object(_read_exact(stream, size).decode("utf-8"))
    return BrowserPacket(message, _read_exact(stream, blob_size))


def _write_all(stream: WriteStream, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = stream.write(remaining)
        if written is None or written <= 0:
            raise OSError("browser worker pipe stopped accepting frame bytes")
        remaining = remaining[written:]


def write_packet(stream: WriteStream, packet: BrowserPacket) -> None:
    data = json.dumps(packet.message, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if not 0 < len(data) <= MAX_MESSAGE_BYTES or len(packet.blob) > MAX_BLOB_BYTES:
        raise ValueError("browser message exceeds transport bounds")
    _write_all(stream, _HEADER.pack(PROTOCOL_VERSION, len(data), len(packet.blob)))
    _write_all(stream, data)
    _write_all(stream, packet.blob)
    stream.flush()
