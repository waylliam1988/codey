"""Test adapter for callers that still provide raw text fixtures.

Production callers must decode text before entering the kernel.  Keeping this
adapter in the test support package lets legacy fixture tests exercise the
same decoder without reintroducing a raw-string kernel entry point.
"""

from __future__ import annotations

from typing import Any

from codey.operations.kernel_protocol import normalize_turn as _normalize_turn
from codey.protocols.text_turn import decode_text_turn


def normalize_turn(reply: object, **kwargs: Any) -> Any:
    decoded = decode_text_turn(reply) if isinstance(reply, str) else reply
    return _normalize_turn(decoded, **kwargs)
