"""Bounded immutable, session-scoped storage for explicitly selected user files."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from pathlib import Path

from codey.storage.file_lock import with_file_lock
from codey.storage.local_store import read_json_strict, session_key, write_json_atomic
from codey.task.attachments import MAX_ATTACHMENT_BYTES, public_attachment, validate_attachments

MAX_SESSION_BYTES = 32 * 1024 * 1024


class InputAttachmentStore:
    def __init__(self, home: Path, session_id: str) -> None:
        if not isinstance(session_id, str) or not session_id or len(session_id) > 512:
            raise ValueError("Invalid attachment session.")
        self.root = Path(home) / "input_attachments" / session_key(session_id)

    def put(self, name: object, encoded: object) -> dict[str, object]:
        if not isinstance(name, str) or not name or len(name) > 255 or any(c in name for c in "\x00/\\\r\n"):
            raise ValueError("Invalid attachment filename.")
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_ATTACHMENT_BYTES + 2) // 3):
            raise ValueError("File exceeds the 2 MiB limit.")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid attachment data.") from exc
        if not data or len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError("File must contain between 1 byte and 2 MiB.")
        mime = _media_type(data)
        if not mime and Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            raise ValueError("Image file is invalid. Choose a valid PNG, JPEG, GIF, or WebP image.")
        if mime:
            width, height = _image_size(data, mime)
            if not width or not height or width * height > 4_194_304 or max(width, height) > 4096:
                raise ValueError("Image must be valid and contain at most 4 megapixels, with each side at most 4096 pixels.")
            content = {"data_url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}
        else:
            if name.lower().endswith(".svg"):
                raise ValueError("SVG images are not supported. Choose PNG, JPEG, GIF, or WebP.")
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValueError("Unsupported file type. Choose a UTF-8 text file or PNG, JPEG, GIF, or WebP image.") from exc
            if any(ord(char) < 32 and char not in "\t\n\r" for char in text):
                raise ValueError("Binary files are not supported.")
            mime, content = "text/plain", {"text": text}
        digest = hashlib.sha256(data).hexdigest()
        identity = "att_" + hashlib.sha256((name + "\0" + digest).encode()).hexdigest()
        item = {"id": identity, "name": name, "media_type": mime, "size": len(data), "sha256": digest, **content}
        if mime == "text/plain":
            item["text_sha256"] = hashlib.sha256(str(content["text"]).encode()).hexdigest()
        validate_attachments([item])
        self.root.mkdir(parents=True, exist_ok=True)
        with with_file_lock(self.root / ".lock"):
            target = self.root / (identity + ".json")
            if not target.exists():
                used = sum(p.stat().st_size for p in self.root.glob("att_*.json"))
                if used + len(json.dumps(item, ensure_ascii=False).encode()) > MAX_SESSION_BYTES:
                    raise ValueError("This chat's attachment storage is full. Start a new chat.")
                write_json_atomic(target, item)
        return public_attachment(item)

    def read(self, identity: object) -> dict[str, object]:
        if not isinstance(identity, str) or not re.fullmatch(r"att_[a-f0-9]{64}", identity):
            raise ValueError("Invalid attachment identity.")
        item = read_json_strict(self.root / (identity + ".json"), max_bytes=3 * MAX_ATTACHMENT_BYTES)
        if item is None:
            raise FileNotFoundError("Attachment not found in this chat.")
        result = validate_attachments([item])[0]
        if result["id"] != identity:
            raise ValueError("Attachment identity does not match stored content.")
        expected_id = "att_" + hashlib.sha256((str(result["name"]) + "\0" + str(result["sha256"])).encode()).hexdigest()
        if expected_id != identity:
            raise ValueError("Attachment metadata is corrupted.")
        raw = str(result.get("text", "")).encode("utf-8") if result["media_type"] == "text/plain" else base64.b64decode(str(result["data_url"]).split(",", 1)[1], validate=True)
        # UTF-8 BOM is normalized at import; image bytes remain exact.
        expected = result.get("text_sha256") if result["media_type"] == "text/plain" else result["sha256"]
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("Attachment content is corrupted.")
        return result


def _media_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _image_size(data: bytes, mime: str) -> tuple[int, int]:
    if mime == "image/png" and len(data) >= 24 and data[12:16] == b"IHDR":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if mime == "image/gif" and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if mime == "image/webp" and len(data) >= 30:
        if data[12:16] == b"VP8X":
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if data[12:16] == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            return int.from_bytes(data[26:28], "little") & 0x3fff, int.from_bytes(data[28:30], "little") & 0x3fff
        if data[12:16] == b"VP8L" and data[20] == 0x2f:
            packed = int.from_bytes(data[21:25], "little")
            return (packed & 0x3fff) + 1, ((packed >> 14) & 0x3fff) + 1
    if mime == "image/jpeg":
        offset = 2
        while offset + 4 <= len(data):
            if data[offset] != 0xff:
                offset += 1
                continue
            marker = data[offset + 1]
            offset += 2
            if marker in {0x01, 0xd8, 0xd9} or 0xd0 <= marker <= 0xd7:
                continue
            length = int.from_bytes(data[offset:offset + 2], "big")
            if length < 2 or offset + length > len(data):
                break
            if marker in {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf} and length >= 8:
                return int.from_bytes(data[offset + 5:offset + 7], "big"), int.from_bytes(data[offset + 3:offset + 5], "big")
            offset += length
    return 0, 0
