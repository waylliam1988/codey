"""Shared, stable identity for one CodeMode parent execution."""
from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar

from codey.runtime.core.models import ToolCall

rpc_child_index: ContextVar[int | None] = ContextVar("codemode_child_index", default=None)



def parent_execution_id(run_id: object, turn: int, call: ToolCall) -> str:
    digest = hashlib.sha256(json.dumps(call.args, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:16]
    return f'{run_id}:turn:{turn}:codemode:{call.call_id or digest}'
