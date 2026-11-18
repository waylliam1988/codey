"""One failure boundary for optional memory; management failures stay visible."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from codey.memory.contracts import (
    DeleteResult,
    DeleteScopeRequest,
    MemoryCapabilities,
    MemoryDecision,
    MemoryEvent,
    MemoryItem,
    MemoryProposal,
    MemoryQuery,
    MemoryResult,
)
from codey.runtime.core.cancellation import DeadlineExceeded, TaskCancelled
from codey.types.json_value import JsonValue

_LOG = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def valid_at(value: str, as_of: str) -> bool:
    if not value:
        return True
    try:
        expiry, now = datetime.fromisoformat(value), datetime.fromisoformat(as_of)
        return expiry.tzinfo is not None and now.tzinfo is not None and expiry > now
    except ValueError:
        return False


class MemoryService:
    def __init__(self, capabilities: MemoryCapabilities, *,
                 source_available: Callable[[str], bool], clock: Callable[[], str] = utc_now) -> None:
        self._capabilities = capabilities
        self._source_available = source_available
        self.clock = clock

    def admissible(self, item: MemoryItem, query: MemoryQuery) -> bool:
        try:
            created = datetime.fromisoformat(item.created_at)
            as_of = datetime.fromisoformat(query.as_of)
        except ValueError:
            return False
        return (created.tzinfo is not None and as_of.tzinfo is not None and created <= as_of
                and bool(item.item_id and item.text and item.source_refs)
                and item.scope in query.scopes and valid_at(item.expires_at, self.clock())
                and all(self._source_available(ref) for ref in item.source_refs))

    def retrieve(self, query: MemoryQuery) -> MemoryResult:
        if query.max_items <= 0:
            return MemoryResult()
        try:
            result = self._capabilities.reader.retrieve(query)
            return MemoryResult(tuple(item for item in result.items if self.admissible(item, query))[:query.max_items])
        except (TaskCancelled, DeadlineExceeded):
            raise
        except Exception:
            _LOG.exception("optional memory retrieval failed")
            return MemoryResult()

    def validate(self, items: tuple[MemoryItem, ...], query: MemoryQuery) -> bool:
        current = self.retrieve(query).items
        return all(item in current for item in items)

    def observe(self, event: MemoryEvent) -> bool:
        try:
            self._capabilities.writer.observe(event)
            return True
        except (TaskCancelled, DeadlineExceeded):
            raise
        except Exception:
            _LOG.exception("optional memory observation failed")
            return False

    def proposals(self, scope: str) -> tuple[MemoryProposal, ...]:
        return self._capabilities.reviewer.proposals(scope)

    def decide(self, decision: MemoryDecision) -> None:
        self._capabilities.reviewer.decide(decision)

    def delete_scope(self, request: DeleteScopeRequest) -> DeleteResult:
        return self._capabilities.admin.delete_scope(request)

    def export_state(self) -> dict[str, JsonValue]:
        return self._capabilities.admin.export_state()

    def maintain(self, *, as_of: str) -> bool:
        try:
            self._capabilities.maintainer.maintain(as_of=as_of)
            return True
        except (TaskCancelled, DeadlineExceeded):
            raise
        except Exception:
            _LOG.exception("optional memory maintenance failed")
            return False
