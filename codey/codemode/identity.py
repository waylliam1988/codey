"""Shared, stable identity for one CodeMode parent execution."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextvars import ContextVar

from codey.runtime.core.models import ToolCall

rpc_child_index: ContextVar[int | None] = ContextVar("codemode_child_index", default=None)
rpc_child_allocator: ContextVar[Callable[[int], tuple[int, ...]] | None] = ContextVar("codemode_child_allocator", default=None)
rpc_child_admission: ContextVar[Callable[[], None] | None] = ContextVar("codemode_child_admission", default=None)


def child_call_index(execution_id: str, call_id: str) -> int:
    prefix = execution_id + ":child:"
    raw_index = call_id.removeprefix(prefix)
    if not call_id.startswith(prefix) or not raw_index.isascii() or not raw_index.isdecimal():
        raise ValueError("invalid CodeMode child identity")
    index = int(raw_index)
    if str(index) != raw_index:
        raise ValueError("invalid CodeMode child identity")
    return index


def parent_execution_id(run_id: object, turn: int, call: ToolCall) -> str:
    digest = hashlib.sha256(json.dumps(call.args, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:16]
    return f'{run_id}:turn:{turn}:codemode:{call.call_id or digest}'
