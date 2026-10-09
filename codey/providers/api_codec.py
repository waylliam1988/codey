"""Small stateless protocol boundary; the provider owns all session state."""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

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


class ApiCodec(Protocol):
    endpoint: str

    def encode_results(self, results: list[ProviderToolResult]) -> list[dict[str, Any]]: ...

    def prepare(self, history: list[dict[str, Any]], pending: list[dict[str, Any]], *, system: str,
                tools: list[ProviderToolDefinition] | None) -> list[dict[str, Any]]: ...

    def validate_view(self, items: list[dict[str, Any]]) -> None: ...

    def closed_spans(self, items: list[dict[str, Any]]) -> list[tuple[int, int]]: ...

    def checkpoint_item(self, text: str) -> dict[str, Any]: ...

    def build_payload(self, items: list[dict[str, Any]], tools: list[ProviderToolDefinition] | None,
                      settings: GenerationSettings) -> dict[str, Any]: ...

    def decode_exchange(self, body: dict[str, Any], *, text_only: bool,
                        text_decoder: Callable[[str], str | AssistantTurn] | None,
                        ) -> tuple[AssistantTurn, list[dict[str, Any]] | None]: ...


class AuxiliaryConnection(Protocol):
    def count_text(self, text: str, *, deadline: float) -> RequestContextCount: ...

    def send(self, text: str, timeout: float | None = None) -> str: ...

    def close(self) -> None: ...
