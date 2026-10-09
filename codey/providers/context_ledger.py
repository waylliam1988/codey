"""Accepted protocol events and replaceable views, separate from execution facts.

Journal segments are immutable after the head commits. A failed head write leaves
an uncommitted segment, never a partially updated live view. No credentials live
here. Provider identity scopes opaque protocol state; semantic text is portable.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from codey.storage.file_lock import with_file_lock
from codey.storage.local_store import read_json_strict, session_key, write_json_atomic

MAX_SEGMENT_BYTES = 16 * 1024 * 1024


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class ContextLedger:
    def __init__(self, root: Path | None = None, *, identity: str = "") -> None:
        self.root = root
        self.identity = identity
        self.sequence = 0
        self.chain = ""
        self.view: list[dict[str, Any]] = []
        self._records: list[dict[str, Any]] = []
        if root is not None and (root / "head.json").exists():
            head = read_json_strict(root / "head.json", max_bytes=MAX_SEGMENT_BYTES)
            if not isinstance(head, dict) or head.get("identity") != identity:
                raise ValueError("context ledger identity mismatch")
            sequence = head.get("sequence")
            if type(sequence) is not int or sequence < 0:
                raise ValueError("invalid context journal sequence")
            for index in range(1, sequence + 1):
                row = read_json_strict(root / f"{index:012d}.json", max_bytes=MAX_SEGMENT_BYTES)
                if not isinstance(row, dict) or row.get("previous") != self.chain or row.get("sequence") != index:
                    raise ValueError("context journal chain mismatch")
                self.chain = digest(row)
                self._records.append(row)
            if self.chain != head.get("chain") or digest(head.get("view")) != head.get("view_digest"):
                raise ValueError("context ledger head digest mismatch")
            self.sequence = sequence
            self.view = head["view"]

    @classmethod
    def for_session(cls, home: Path, session_id: str, identity: str) -> ContextLedger:
        return cls(home / "context_history" / session_key(session_id) / identity, identity=identity)

    def events(self) -> list[dict[str, Any]]:
        return copy.deepcopy([item for row in self._records for item in row.get("events", [])])

    def commit(self, view: list[dict[str, Any]], *, events: list[dict[str, Any]] | None = None,
               checkpoint: dict[str, Any] | None = None) -> None:
        row = copy.deepcopy({"sequence": self.sequence + 1, "previous": self.chain,
                             "events": events or [], "checkpoint": checkpoint, "created_at": time.time()})
        chain = digest(row)
        if self.root is not None:
            with with_file_lock(self.root):
                head_path = self.root / "head.json"
                if head_path.exists():
                    head = read_json_strict(head_path, max_bytes=MAX_SEGMENT_BYTES)
                    if not isinstance(head, dict) or head.get("sequence") != self.sequence or head.get("chain") != self.chain:
                        raise ValueError("context journal changed in another writer")
                elif self.sequence:
                    raise ValueError("context journal head disappeared")
                write_json_atomic(self.root / f"{self.sequence + 1:012d}.json", row, max_bytes=MAX_SEGMENT_BYTES)
                write_json_atomic(head_path, {"identity": self.identity, "sequence": self.sequence + 1,
                                  "chain": chain, "view": view, "view_digest": digest(view)}, max_bytes=MAX_SEGMENT_BYTES)
        self.sequence += 1
        self.chain = chain
        self._records.append(row)
        self.view = copy.deepcopy(view)

    def checkpoint_digests(self) -> frozenset[str]:
        return frozenset(checkpoint["item_digest"] for checkpoint in self.checkpoints()
                         if checkpoint.get("item_digest"))

    def checkpoints(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for row in self._records:
            checkpoint = row.get("checkpoint")
            if checkpoint is not None:
                output.extend(checkpoint["segments"] if checkpoint.get("kind") == "transaction" else [checkpoint])
        return output

    def original_source(self, items: list[dict[str, Any]], *,
                        checkpoints: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Expand prior checkpoints from their sources, never summarize a summary."""
        sources: dict[str, list[tuple[int, list[dict[str, Any]]]]] = {}
        lineage = self.checkpoints() + (checkpoints or [])
        for index, checkpoint in enumerate(lineage):
            if checkpoint and checkpoint.get("item_digest"):
                sources.setdefault(checkpoint["item_digest"], []).append((index, checkpoint["source"]))
        output: list[dict[str, Any]] = []
        pending = [(item, len(lineage)) for item in reversed(items)]
        while pending:
            item, before = pending.pop()
            versions = [(index, source) for index, source in sources.get(digest(item), []) if index < before]
            if not versions:
                output.append(copy.deepcopy(item))
            else:
                index, source = versions[-1]
                # Strictly older lineage also handles two identical answers:
                # a checkpoint can never expand into its own same-text record.
                pending.extend((entry, index) for entry in reversed(source))
        return output

    def portable_state(self) -> str:
        """Read verified semantic checkpoints across this session's model identities."""
        ledgers = [self]
        if self.root is not None:
            ledgers += [ContextLedger(path.parent, identity=path.parent.name)
                        for path in self.root.parent.glob("*/head.json") if path.parent != self.root]
        populated: list[ContextLedger] = [ledger for ledger in ledgers if ledger.view]
        if not populated:
            return ""
        latest = max(populated, key=lambda ledger: ledger._records[-1].get("created_at", 0))
        parts: list[str] = []
        checkpoints = latest.checkpoint_digests()
        retained = [item for index, item in enumerate(latest.view)
                    if index >= len(latest.view) - 12 or digest(item) in checkpoints]
        for item in retained:
            role = item.get("role", item.get("type", "record"))
            value = item.get("content", item.get("output", ""))
            if isinstance(value, list):
                value = "\n".join(str(part.get("text", "")) for part in value if isinstance(part, dict) and part.get("type") in {"output_text", "text"})
            if isinstance(value, str) and value:
                parts.append(f"{role}: {value}")
            if item.get("type") == "function_call":
                parts.append(f"Recorded call: {item.get('name')} id={item.get('call_id')}")
        return "Current project state:\n" + "\n\n".join(parts)
