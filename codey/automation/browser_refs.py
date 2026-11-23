"""Bounded identities for observed nodes; labels never rediscover a target."""
from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Generic, TypeVar
from uuid import uuid4

from codey.automation.browser_document import same_document
from codey.types.json_value import JsonObject

Handle = TypeVar("Handle")


@dataclass(frozen=True)
class ElementRecord(Generic[Handle]):
    handle: Handle
    document: JsonObject
    backend_node_id: int
    role: str
    name: str
    context: JsonObject | None
    context_node: Handle | None
    query_target: JsonObject | None
    revision: int
    semantics_json: str = ""
    context_backend_node_id: int = 0
    scope_only: bool = False


class ElementRegistry(Generic[Handle]):
    def __init__(self, *, capacity: int = 320) -> None:
        if capacity < 1:
            raise ValueError("element registry capacity must be positive")
        self.capacity = capacity
        self.records: OrderedDict[str, ElementRecord[Handle]] = OrderedDict()
        self._scope = uuid4().hex[:12]
        self._sequence = 0
        self._retired: list[ElementRecord[Handle]] = []

    def observe_node(self, record: ElementRecord[Handle]) -> str:
        if record.backend_node_id <= 0 or not record.document:
            raise ValueError("a node requires an identified document and backend node")
        for ref, previous in tuple(self.records.items()):
            if (same_document(previous.document, record.document)
                    and previous.backend_node_id == record.backend_node_id
                    and previous.scope_only == record.scope_only
                    and previous.role == record.role and previous.name == record.name
                    and previous.semantics_json == record.semantics_json
                    and previous.context_backend_node_id == record.context_backend_node_id
                    and self._context_key(previous.context) == self._context_key(record.context)):
                if previous is not record:
                    self._retired.append(previous)
                self.records[ref] = record
                self.records.move_to_end(ref)
                return ref
        self._sequence += 1
        ref = f"b{self._scope}e{self._sequence}"
        self.records[ref] = record
        while len(self.records) > self.capacity:
            self._retired.append(self.records.popitem(last=False)[1])
        return ref

    @staticmethod
    def _context_key(context: JsonObject | None) -> str:
        return json.dumps({key: value for key, value in (context or {}).items() if key != "id"},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def resolve(self, ref: str, document: JsonObject | None) -> ElementRecord[Handle]:
        record = self.records.get(ref)
        if record is None:
            raise ValueError("unknown_ref")
        if not same_document(record.document, document):
            raise ValueError("stale_ref")
        return record

    def observed_context(self, identifier: str, document: JsonObject) -> ElementRecord[Handle] | None:
        return next((record for record in reversed(self.records.values())
                     if record.context is not None and record.context_node is not None
                     and identifier == f"region-{record.context_backend_node_id}"
                     and same_document(record.document, document)), None)

    def invalidate_document(self, document: JsonObject) -> None:
        for ref, record in tuple(self.records.items()):
            if same_document(record.document, document):
                self._retired.append(record)
                del self.records[ref]

    def invalidate_all(self) -> None:
        self._retired.extend(self.records.values())
        self.records.clear()

    def retired_handles(self) -> tuple[Handle, ...]:
        live = {id(handle) for record in self.records.values()
                for handle in (record.handle, record.context_node) if handle is not None}
        retired = {id(handle): handle for record in self._retired
                   for handle in (record.handle, record.context_node) if handle is not None and id(handle) not in live}
        self._retired.clear()
        return tuple(retired.values())
