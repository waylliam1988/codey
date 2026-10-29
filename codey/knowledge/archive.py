"""Stable knowledge identities; independent of research observations."""

import hashlib

from codey.knowledge.model import canonical_json


def stable_identity(kind: str, *parts: str) -> str:
    return kind + "-" + hashlib.sha256(canonical_json(parts).encode()).hexdigest()
