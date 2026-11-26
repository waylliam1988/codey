"""Callable that executes one canonical tool call."""
from collections.abc import Callable

from codey.runtime.core.models import ToolCall, ToolResult

ToolExecutor = Callable[[ToolCall], ToolResult]
