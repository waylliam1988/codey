"""Bounded subprocess output capture.

One instance per stream (stdout / stderr). Reads continue to drain the
pipe so children never block, but only head + tail bytes are retained.
"""

from __future__ import annotations

import codecs
import locale
import os
from collections import deque
from dataclasses import dataclass
from typing import SupportsIndex, SupportsInt, TypeAlias, cast

_INT_INPUT: TypeAlias = str | bytes | bytearray | SupportsInt | SupportsIndex

HEAD_LIMIT_BYTES = 64 * 1024
TAIL_LIMIT_BYTES = 192 * 1024
CAPTURE_LIMIT_BYTES = HEAD_LIMIT_BYTES + TAIL_LIMIT_BYTES
READ_CHUNK_BYTES = 8 * 1024
DRAIN_TIMEOUT_SECONDS = 2.0
READER_CLEANUP_TIMEOUT_SECONDS = 2.0


def _safe_limit(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(cast(_INT_INPUT, value))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, parsed)


@dataclass(frozen=True)
class CapturedText:
    text: str
    total_bytes: int
    truncated: bool
    omitted_bytes: int
    decoding_errors: bool = False
    encoding: str = "utf-8"
    head: str = ""
    tail: str = ""


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
        fallback_encoding: str | None = None,
    ) -> None:
        self._head_limit = _safe_limit(head_limit, HEAD_LIMIT_BYTES)
        self._tail_limit = _safe_limit(tail_limit, TAIL_LIMIT_BYTES)
        self._head = bytearray()
        self._tail: deque[bytes] = deque()
        self._tail_len = 0
        self._total = 0
        fallback = fallback_encoding or (locale.getencoding() if os.name == "nt" else "utf-8")
        self._encodings = tuple(dict.fromkeys(("utf-8", codecs.lookup(fallback).name)))
        self._validators = {name: codecs.getincrementaldecoder(name)(errors="strict") for name in self._encodings}
        self._prefix = {name: codecs.getincrementaldecoder(name)(errors="replace") for name in self._encodings}
        self._invalid: set[str] = set()

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._total += len(chunk)
        for name, decoder in self._validators.items():
            if name in self._invalid:
                continue
            try:
                decoder.decode(chunk)
            except UnicodeDecodeError:
                self._invalid.add(name)
        view = chunk
        if len(self._head) < self._head_limit:
            need = self._head_limit - len(self._head)
            self._head.extend(view[:need])
            self._advance_prefix(view[:need])
            view = view[need:]
            if not view:
                return
        self._tail.append(view)
        self._tail_len += len(view)
        while self._tail_len > self._tail_limit:
            excess = self._tail_len - self._tail_limit
            left = self._tail[0]
            if len(left) <= excess:
                self._tail.popleft()
                self._tail_len -= len(left)
                self._advance_prefix(left)
            else:
                self._tail[0] = left[excess:]
                self._tail_len -= excess
                self._advance_prefix(left[:excess])

    def _advance_prefix(self, raw: bytes) -> None:
        for decoder in self._prefix.values():
            decoder.decode(raw)

    @property
    def total_bytes(self) -> int:
        return self._total

    def finish(self, *, final: bool = True) -> CapturedText:
        """A repeatable snapshot; stream validation includes discarded bytes."""
        encoding = "utf-8"
        lossy = True
        for name, validator in self._validators.items():
            if name in self._invalid:
                continue
            probe = codecs.getincrementaldecoder(name)(errors="strict")
            probe.setstate(validator.getstate())
            try:
                probe.decode(b"", final=final)
            except UnicodeDecodeError:
                continue
            encoding, lossy = name, False
            break
        warning = ("[encoding warning: invalid UTF-8; displayed text is lossy. Configure the program to emit UTF-8.]\n"
                   if lossy else "")
        if not lossy and encoding != "utf-8":
            warning = f"[encoding: {encoding}; UTF-8 was invalid, using the host code page.]\n"
        head = bytes(self._head)
        tail = b"".join(self._tail)
        if self._total <= len(head) + len(tail):
            decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
            text = decoder.decode(head + tail, final=final)
            return CapturedText(
                text=warning + text,
                total_bytes=self._total,
                truncated=False,
                omitted_bytes=0,
                decoding_errors=lossy, encoding=encoding, head=text,
            )
        head_decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        head_text = head_decoder.decode(head, final=False)
        pending = len(head_decoder.getstate()[0])
        head = head[:len(head) - pending]
        if encoding == "utf-8":
            tail = _trim_leading_incomplete(tail)
        else:
            # The state immediately before the retained suffix identifies
            # a split DBCS character even when its trail byte looks like ASCII.
            boundary = codecs.getincrementaldecoder(encoding)(errors="replace")
            boundary.setstate(self._prefix[encoding].getstate())
            offset = 0
            while boundary.getstate()[0] and offset < len(tail):
                boundary.decode(tail[offset:offset + 1])
                offset += 1
            tail = tail[offset:]
        tail_decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        tail_text = tail_decoder.decode(tail, final=final)
        # Report what the displayed text actually omits: raw middle bytes
        # plus edge bytes dropped to avoid splitting a UTF-8 character.
        omitted = self._total - len(head) - len(tail)
        marker = (
            f"\n[... omitted {omitted} bytes of output; "
            "showing head and tail only ...]\n"
        )
        return CapturedText(
            text=f"{warning}{head_text}{marker}{tail_text}",
            total_bytes=self._total,
            truncated=True,
            omitted_bytes=omitted,
            decoding_errors=lossy, encoding=encoding, head=head_text, tail=tail_text,
        )


__all__ = [
    "CAPTURE_LIMIT_BYTES",
    "DRAIN_TIMEOUT_SECONDS",
    "HEAD_LIMIT_BYTES",
    "READ_CHUNK_BYTES",
    "READER_CLEANUP_TIMEOUT_SECONDS",
    "TAIL_LIMIT_BYTES",
    "BoundedByteCapture",
    "CapturedText",
]
