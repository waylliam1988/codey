"""Bounded subprocess output capture.

One instance per stream (stdout / stderr). Reads continue to drain the
pipe so children never block, but only head + tail bytes are retained.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

HEAD_LIMIT_BYTES = 64 * 1024
TAIL_LIMIT_BYTES = 192 * 1024
CAPTURE_LIMIT_BYTES = HEAD_LIMIT_BYTES + TAIL_LIMIT_BYTES
READ_CHUNK_BYTES = 8 * 1024
DRAIN_TIMEOUT_SECONDS = 2.0
READER_JOIN_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class CapturedText:
    text: str
    total_bytes: int
    truncated: bool
    omitted_bytes: int


def _trim_trailing_incomplete(data: bytes) -> bytes:
    """Drop a trailing incomplete UTF-8 sequence (cold start: no compat)."""
    if not data:
        return data
    # Count trailing continuation bytes.
    cont = 0
    for byte in reversed(data[-4:]):
        if 0x80 <= byte <= 0xBF:
            cont += 1
        else:
            break
    if cont == 0:
        # Last byte is ASCII or a complete start byte; check if it starts
        # a multi-byte sequence that needs more bytes.
        last = data[-1]
        if last <= 0x7F or 0xC2 <= last <= 0xF4:
            # A start byte at the very end with no continuations is
            # incomplete when it expects more bytes.
            if last >= 0xC2:
                return data[:-1]
            return data
        # Invalid byte: let the decoder replace it, keep it visible.
        return data
    if len(data) <= cont:
        return b""
    start = data[-(cont + 1)]
    if 0xC2 <= start <= 0xDF:
        expected = 2
    elif 0xE0 <= start <= 0xEF:
        expected = 3
    elif 0xF0 <= start <= 0xF4:
        expected = 4
    else:
        # Orphan continuations (no valid start): drop them.
        return data[: len(data) - cont]
    if cont + 1 < expected:
        return data[: len(data) - (cont + 1)]
    return data


def _trim_leading_incomplete(data: bytes) -> bytes:
    """Drop leading orphan continuation bytes from a tail slice."""
    index = 0
    while index < len(data) and 0x80 <= data[index] <= 0xBF and index < 4:
        index += 1
        # Stop at the first start byte; a start byte at the tail head is
        # complete because the tail is a contiguous suffix.
        if index < len(data) and not (0x80 <= data[index] <= 0xBF):
            break
    # Only strip when the slice actually started inside a character, i.e.
    # the first byte was a continuation byte.
    if data and 0x80 <= data[0] <= 0xBF:
        return data[index:]
    return data


class BoundedByteCapture:
    """Retain first HEAD + last TAIL bytes; count every byte read."""

    def __init__(
        self,
        *,
        head_limit: int = HEAD_LIMIT_BYTES,
        tail_limit: int = TAIL_LIMIT_BYTES,
    ) -> None:
        self._head_limit = max(0, int(head_limit))
        self._tail_limit = max(0, int(tail_limit))
        self._head = bytearray()
        self._tail: deque[bytes] = deque()
        self._tail_len = 0
        self._total = 0

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._total += len(chunk)
        view = chunk
        if len(self._head) < self._head_limit:
            need = self._head_limit - len(self._head)
            self._head.extend(view[:need])
            view = view[need:]
            if not view:
                return
        self._tail.append(bytes(view))
        self._tail_len += len(view)
        while self._tail_len > self._tail_limit:
            excess = self._tail_len - self._tail_limit
            left = self._tail[0]
            if len(left) <= excess:
                self._tail.popleft()
                self._tail_len -= len(left)
            else:
                self._tail[0] = left[excess:]
                self._tail_len -= excess

    @property
    def total_bytes(self) -> int:
        return self._total

    def finish(self) -> CapturedText:
        head = bytes(self._head)
        tail = b"".join(self._tail)
        if self._total <= len(head) + len(tail):
            text = (head + tail).decode("utf-8", errors="replace")
            return CapturedText(
                text=text,
                total_bytes=self._total,
                truncated=False,
                omitted_bytes=0,
            )
        head = _trim_trailing_incomplete(head)
        tail = _trim_leading_incomplete(tail)
        head_text = head.decode("utf-8", errors="replace")
        tail_text = tail.decode("utf-8", errors="replace")
        # Report what the displayed text actually omits: raw middle bytes
        # plus edge bytes dropped to avoid splitting a UTF-8 character.
        omitted = self._total - len(head) - len(tail)
        marker = (
            f"\n[... omitted {omitted} bytes of output; "
            "showing head and tail only ...]\n"
        )
        return CapturedText(
            text=f"{head_text}{marker}{tail_text}",
            total_bytes=self._total,
            truncated=True,
            omitted_bytes=omitted,
        )


__all__ = [
    "CAPTURE_LIMIT_BYTES",
    "DRAIN_TIMEOUT_SECONDS",
    "HEAD_LIMIT_BYTES",
    "READ_CHUNK_BYTES",
    "READER_JOIN_TIMEOUT_SECONDS",
    "TAIL_LIMIT_BYTES",
    "BoundedByteCapture",
    "CapturedText",
]
