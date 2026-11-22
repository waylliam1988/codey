"""Small stateless protocol boundary; the provider owns all session state."""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from codey.providers.base import AssistantTurn, ProviderToolDefinition, ProviderToolResult
from codey.providers.token_accounting import RequestContextCount


@dataclass(frozen=True)
class GenerationSettings:
    model: str
    stream: bool
    temperature: float
    thinking: bool | None
    effort: str | None
    choice: str
    output_tokens: int | None
    text_tool_json: bool = False
    text_tool_schema: dict[str, object] | None = None
    text_tool_contract: str = ""
    text_tool_observations: str = ""
    tool_history_groups: int = 0


class ApiCodec(Protocol):
    @property
    def endpoint(self) -> str: ...

    def encode_context(self, messages: list[dict[str, object]]) -> list[dict[str, object]]: ...

    def encode_results(self, results: list[ProviderToolResult]) -> list[dict[str, object]]: ...

    def prepare(self, history: list[dict[str, object]], pending: list[dict[str, object]], *, system: str,
                tools: list[ProviderToolDefinition] | None) -> list[dict[str, object]]: ...

    def validate_view(self, items: list[dict[str, object]]) -> None: ...

    def pending_calls(self, items: list[dict[str, object]]) -> tuple[str, ...]: ...

    def closed_spans(self, items: list[dict[str, object]]) -> list[tuple[int, int]]: ...

    def checkpoint_item(self, text: str) -> dict[str, object]: ...

    def build_payload(self, items: list[dict[str, object]], tools: list[ProviderToolDefinition] | None,
                      settings: GenerationSettings, /) -> dict[str, object]: ...

    def decode_exchange(self, body: dict[str, object], *, text_only: bool,
                        text_decoder: Callable[[str], str | AssistantTurn] | None,
                        ) -> tuple[AssistantTurn, list[dict[str, object]] | None]: ...


class AuxiliaryConnection(Protocol):
    def count_text(self, text: str, *, deadline: float) -> RequestContextCount: ...

    def send(self, text: str, timeout: float | None = None) -> str: ...

    def close(self) -> None: ...
