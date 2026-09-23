"""Tool registry snapshot: single source for JSON prompts and native schemas.

Cold-start friendly. The static ``TOOL_DEFINITIONS`` tuple remains the seed;
the registry only freezes a profile/mode-filtered view so prompts, native
schemas, and validation cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from codey.toolchain import definition as tool_defs


@dataclass(frozen=True)
class ToolRegistrySnapshot:
    profile_name: str
    mode: str
    definitions: tuple[tool_defs.ToolDefinition, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(d.name for d in self.definitions)

    @property
    def runtime_names(self) -> tuple[str, ...]:
        return tuple(d.runtime_name for d in self.definitions if d.runtime_name is not None)


class ToolRegistry:
    def __init__(self, definitions: Sequence[tool_defs.ToolDefinition] | None = None) -> None:
        self._definitions = tuple(definitions) if definitions is not None else tool_defs.TOOL_DEFINITIONS

    def register(self, definition: tool_defs.ToolDefinition) -> None:
        names = {definition.name, *definition.aliases}
        for existing in self._definitions:
            if existing.name == definition.name or names & {existing.name, *existing.aliases}:
                raise ValueError(f"duplicate tool contract name: {definition.name}")
        self._definitions = (*self._definitions, definition)

    def snapshot(self, *, profile_name: str = "coding_writer", mode: str = "coding") -> ToolRegistrySnapshot:
        from codey.policies.permissions import allowed_coding_tool_names, profile_for_name

        try:
            profile = profile_for_name(profile_name)
            allowed = set(allowed_coding_tool_names(profile))
        except Exception:
            allowed = {d.name for d in self._definitions} | {
                alias for d in self._definitions for alias in d.aliases
            }
        definitions = tool_defs.definitions_for_tool_names(tuple(allowed))
        if not definitions:
            definitions = self._definitions
        return ToolRegistrySnapshot(profile_name=profile_name, mode=mode, definitions=definitions)

    def definitions_for_names(self, names: Sequence[str]) -> tuple[tool_defs.ToolDefinition, ...]:
        return tool_defs.definitions_for_tool_names(tuple(names))


DEFAULT_REGISTRY = ToolRegistry()


__all__ = ["DEFAULT_REGISTRY", "ToolRegistry", "ToolRegistrySnapshot"]
