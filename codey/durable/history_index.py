"""Persistent counted trees for display order, entry locations and visible runs.

Nodes are immutable. A display item is a unit source interval; path copying
shares all unaffected intervals. Every raw entry retains its three roots, so
rewinding, editing and inheriting a prefix never rebuild a displayed history.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Protocol

from codey.durable.model import DurableMutation, EntryDraft

HIDDEN_KINDS = frozenset({"context_snapshot", "control_config", "control_receipt"})


def rewrites_path(mutation: DurableMutation) -> bool:
    return (mutation.branch_entry_id is not None or mutation.edited_entry_id is not None
            or any(entry.payload.get("head_marker") is True for entry in mutation.entries))


@dataclass(frozen=True)
class DisplayNode:
    key: str
    value: str
    question: bool
    left: str | None
    right: str | None
    height: int
    count: int
    questions: int


@dataclass(frozen=True)
class DisplayVersion:
    order: str | None = None
    locations: str | None = None
    runs: str | None = None
    cut: str = ""


class NodeReader(Protocol):
    def node(self, identity: str) -> DisplayNode: ...


class NodeWriter(NodeReader, Protocol):
    def put_node(self, node: DisplayNode) -> str: ...


def size(reader: NodeReader, root: str | None, *, questions: bool = False) -> int:
    if root is None:
        return 0
    node = reader.node(root)
    return node.questions if questions else node.count


def find(reader: NodeReader, root: str | None, key: str) -> str | None:
    while root is not None:
        node = reader.node(root)
        if node.key == key:
            return node.value
        root = node.left if key < node.key else node.right
    return None


def _height(reader: NodeReader, root: str | None) -> int:
    return 0 if root is None else reader.node(root).height


def _make(writer: NodeWriter, key: str, value: str, question: bool,
          left: str | None, right: str | None) -> str:
    return writer.put_node(DisplayNode(
        key, value, question, left, right, 1 + max(_height(writer, left), _height(writer, right)),
        1 + size(writer, left) + size(writer, right),
        int(question) + size(writer, left, questions=True) + size(writer, right, questions=True),
    ))


def _balance(writer: NodeWriter, key: str, value: str, question: bool,
             left: str | None, right: str | None) -> str:
    difference = _height(writer, left) - _height(writer, right)
    if difference > 1:
        assert left is not None
        child = writer.node(left)
        if _height(writer, child.left) < _height(writer, child.right):
            assert child.right is not None
            middle = writer.node(child.right)
            left = _make(writer, child.key, child.value, child.question, child.left, middle.left)
            right = _make(writer, key, value, question, middle.right, right)
            return _make(writer, middle.key, middle.value, middle.question, left, right)
        right = _make(writer, key, value, question, child.right, right)
        return _make(writer, child.key, child.value, child.question, child.left, right)
    if difference < -1:
        assert right is not None
        child = writer.node(right)
        if _height(writer, child.right) < _height(writer, child.left):
            assert child.left is not None
            middle = writer.node(child.left)
            left = _make(writer, key, value, question, left, middle.left)
            right = _make(writer, child.key, child.value, child.question, middle.right, child.right)
            return _make(writer, middle.key, middle.value, middle.question, left, right)
        left = _make(writer, key, value, question, left, child.left)
        return _make(writer, child.key, child.value, child.question, left, child.right)
    return _make(writer, key, value, question, left, right)


def insert(writer: NodeWriter, root: str | None, key: str, value: str, *, question: bool = False) -> str:
    if root is None:
        return _make(writer, key, value, question, None, None)
    node = writer.node(root)
    if node.key == key:
        if node.value == value and node.question == question:
            return root
        return _make(writer, key, value, question, node.left, node.right)
    if key < node.key:
        left = insert(writer, node.left, key, value, question=question)
        if left == node.left:
            return root
        return _balance(writer, node.key, node.value, node.question, left, node.right)
    right = insert(writer, node.right, key, value, question=question)
    if right == node.right:
        return root
    return _balance(writer, node.key, node.value, node.question, node.left, right)


def window(reader: NodeReader, root: str | None, start: int, end: int,
           *, questions: bool = False) -> Iterator[DisplayNode]:
    """One counted seek followed by traversal; no OFFSET or per-item seeks."""
    if root is None or start >= end:
        return
    node = reader.node(root)
    if questions and node.questions == 0:
        return
    left_count = size(reader, node.left, questions=questions)
    weight = int(node.question) if questions else 1
    if start < left_count:
        yield from window(reader, node.left, start, min(end, left_count), questions=questions)
    if weight and start <= left_count < end:
        yield node
    offset = left_count + weight
    if end > offset:
        yield from window(reader, node.right, max(0, start - offset), end - offset, questions=questions)


def extend(writer: NodeWriter, version: DisplayVersion, entry: EntryDraft) -> DisplayVersion:
    if entry.payload.get("head_marker") is True:
        version = DisplayVersion(locations=version.locations, cut=entry.entry_id)
    if entry.kind in HIDDEN_KINDS:
        return DisplayVersion(version.order,
                              insert(writer, version.locations, entry.entry_id, f"{version.cut}:-1"),
                              version.runs, version.cut)
    ordinal = size(writer, version.order) + 1
    return DisplayVersion(
        insert(writer, version.order, f"{ordinal:020d}", entry.entry_id, question=entry.kind == "user_message"),
        insert(writer, version.locations, entry.entry_id, f"{version.cut}:{ordinal - 1}"),
        insert(writer, version.runs, entry.by_task_id, "1") if entry.by_task_id else version.runs,
        version.cut,
    )


def locate(reader: NodeReader, version: DisplayVersion, entry_id: str) -> int | None:
    value = find(reader, version.locations, entry_id)
    if value is None:
        return None
    cut, ordinal = value.rsplit(":", 1)
    return int(ordinal) if cut == version.cut and int(ordinal) >= 0 else None


class NodeBatch:
    """Stage path copies and discard rotation intermediates before publication."""

    def __init__(self, reader: NodeReader, allocate: Callable[[], str] | None = None) -> None:
        self.reader = reader
        self.nodes: dict[str, DisplayNode] = {}
        self._allocate = allocate

    def node(self, identity: str) -> DisplayNode:
        node = self.nodes.get(identity)
        return self.reader.node(identity) if node is None else node

    def put_node(self, node: DisplayNode) -> str:
        identity = uuid.uuid4().hex if self._allocate is None else self._allocate()
        self.nodes[identity] = node
        return identity

    def reachable(self, versions: list[DisplayVersion]) -> dict[str, DisplayNode]:
        pending = [root for version in versions for root in (version.order, version.locations, version.runs) if root]
        result: dict[str, DisplayNode] = {}
        while pending:
            identity = pending.pop()
            if identity in result or identity not in self.nodes:
                continue
            node = self.nodes[identity]
            result[identity] = node
            pending.extend(child for child in (node.left, node.right) if child)
        return result
