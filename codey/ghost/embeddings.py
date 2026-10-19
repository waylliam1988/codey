"""Explicitly configured embedding endpoint for optional semantic recall."""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class Embeddings(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def encode(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...


def pack_embedding(vector: tuple[float, ...], dimensions: int) -> bytes:
    if len(vector) != dimensions or dimensions < 1 or any(
        type(value) not in {int, float} or not math.isfinite(value) for value in vector
    ):
        raise ValueError("embedding must contain the configured number of finite coordinates")
    if not any(vector):
        raise ValueError("embedding must have a nonzero norm")
    try:
        packed = struct.pack(f"<{dimensions}f", *vector)
    except (OverflowError, struct.error) as exc:
        raise ValueError("embedding is outside float32 range") from exc
    values = struct.unpack(f"<{dimensions}f", packed)
    if not all(math.isfinite(value) for value in values) or not any(values):
        raise ValueError("embedding cannot be represented as finite nonzero float32")
    return packed


@dataclass(frozen=True)
class EmbeddingEndpoint:
    url: str
    model: str
    dimensions: int

    def __post_init__(self) -> None:
        parts = urlsplit(self.url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("Ghost embedding URL must be an HTTP endpoint without embedded credentials")
        if not self.model.strip() or type(self.dimensions) is not int or not 1 <= self.dimensions <= 8192:
            raise ValueError("Ghost embeddings require a model and dimensions between 1 and 8192")

    @property
    def model_id(self) -> str:
        endpoint_id = hashlib.sha256(self.url.encode()).hexdigest()[:16]
        return f"{self.model}@{endpoint_id}"

    def encode(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        headers = {"Content-Type": "application/json"}
        key = os.environ.get("GHOST_EMBEDDING_API_KEY", "")
        if key:
            headers["Authorization"] = "Bearer " + key
        body = json.dumps({"model": self.model, "input": texts}, ensure_ascii=False).encode("utf-8")
        request = Request(self.url, data=body, headers=headers, method="POST")
        with urlopen(request, timeout=10) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("embedding response exceeds its byte budget")
        payload = json.loads(raw)
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise ValueError("embedding response cardinality mismatch")
        ordered: dict[int, tuple[float, ...]] = {}
        for item in data:
            if not isinstance(item, dict) or type(item.get("index")) is not int or not isinstance(item.get("embedding"), list):
                raise ValueError("invalid embedding response item")
            index = item["index"]
            if index in ordered or not 0 <= index < len(texts):
                raise ValueError("embedding response index mismatch")
            vector = tuple(item["embedding"])
            pack_embedding(vector, self.dimensions)
            ordered[index] = vector
        return tuple(ordered[index] for index in range(len(texts)))


def configured_embeddings() -> EmbeddingEndpoint | None:
    url = os.environ.get("GHOST_EMBEDDING_URL", "").strip()
    model = os.environ.get("GHOST_EMBEDDING_MODEL", "").strip()
    dimensions = os.environ.get("GHOST_EMBEDDING_DIMENSIONS", "").strip()
    if not any((url, model, dimensions)):
        return None
    if not all((url, model, dimensions)):
        raise ValueError("configure GHOST_EMBEDDING_URL, GHOST_EMBEDDING_MODEL and GHOST_EMBEDDING_DIMENSIONS together")
    return EmbeddingEndpoint(url, model, int(dimensions))
