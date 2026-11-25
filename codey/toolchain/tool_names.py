"""Stable model and execution names shared by admission and receipt validation."""
from types import MappingProxyType

_RUNTIME_NAME_BY_MODEL_NAME = MappingProxyType({
    "list_dir": "ls",
    "read_file": "read",
    "grep": "search",
    "find_references": "references",
})
MODEL_NAME_BY_RUNTIME_NAME = MappingProxyType({
    runtime: model for model, runtime in _RUNTIME_NAME_BY_MODEL_NAME.items()
})


def runtime_tool_name(model: str) -> str:
    return _RUNTIME_NAME_BY_MODEL_NAME.get(model, model)
