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

from codey.runtime.core.protocol_window import ProtocolWindowRef
from codey.storage.file_lock import with_file_lock
from codey.storage.local_store import read_json_strict, session_key, write_json_atomic

MAX_SEGMENT_BYTES = 16 * 1024 * 1024


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class ContextLedger:
    def __init__(self, root: Path | None = None, *, identity: str = "", window_ref: ProtocolWindowRef | None = None) -> None:
        self.root = root
        self.identity = identity
        self.window_ref = window_ref
        self.sequence = 0
        self.chain = ""
        self.view: list[dict[str, Any]] = []
        self._records: list[dict[str, Any]] = []
        if root is not None and (root / "head.json").exists():
            head = read_json_strict(root / "head.json", max_bytes=MAX_SEGMENT_BYTES)
            if not isinstance(head, dict) or head.get("schema_version") != 1 or head.get("identity") != identity:
                raise ValueError("context ledger identity mismatch")
            expected_session = identity.removeprefix("protocol:") if identity.startswith("protocol:") else ""
            if expected_session and head.get("session_id") != expected_session:
                raise ValueError("context ledger session mismatch")
            stored_ref = head.get("window_ref")
            if stored_ref is not None:
                self.window_ref = ProtocolWindowRef.from_payload(stored_ref)
            sequence = head.get("sequence")
            if type(sequence) is not int or sequence < 0:
                raise ValueError("invalid context journal sequence")
            for index in range(1, sequence + 1):
                row = read_json_strict(root / f"{index:012d}.json", max_bytes=MAX_SEGMENT_BYTES)
                if (not isinstance(row, dict) or row.get("schema_version") != 1
                        or row.get("previous") != self.chain or row.get("sequence") != index):
                    raise ValueError("context journal chain/digest mismatch")
                self.chain = digest(row)
                self._records.append(row)
            if self.chain != head.get("chain") or digest(head.get("view")) != head.get("view_digest"):
                raise ValueError("context ledger head digest mismatch")
            if self.window_ref is not None:
                last_ref = self._records[-1].get("window_ref") if self._records else None
                if (not isinstance(last_ref, dict)
                        or ProtocolWindowRef.from_payload(last_ref) != self.window_ref):
                    raise ValueError("context ledger window reference mismatch")
            self.sequence = sequence
            self.view = head["view"]

    @classmethod
    def for_session(cls, home: Path, session_id: str, identity: str) -> ContextLedger:
        return cls(home / "context_history" / session_key(session_id) / identity, identity=identity)

    @classmethod
    def for_protocol_session(cls, home: Path, session_id: str, ref: ProtocolWindowRef) -> ContextLedger:
        return cls(
            home / "context_history" / session_key(session_id) / "protocol",
            identity=f"protocol:{session_id}",
            window_ref=ref,
        )

    def events(self) -> list[dict[str, Any]]:
        return copy.deepcopy([item for row in self._records for item in row.get("events", [])])

    def restore(self, expected_ref: ProtocolWindowRef | None = None) -> list[dict[str, Any]]:
        """Return the committed view only when the requested window matches."""
        if expected_ref is not None and self.window_ref != expected_ref:
            from codey.runtime.core.protocol_window import WindowMismatch

            raise WindowMismatch("context ledger window reference does not match")
        return copy.deepcopy(self.view)

    def commit(self, view: list[dict[str, Any]], *, events: list[dict[str, Any]] | None = None,
               checkpoint: dict[str, Any] | None = None, window_ref: ProtocolWindowRef | None = None,
               allow_window_change: bool = False, delivery_commit: dict[str, Any] | None = None) -> None:
        previous_ref = self.window_ref
        if (window_ref is not None and self.window_ref is not None
                and self.window_ref != window_ref and not allow_window_change):
            raise ValueError("context ledger window mismatch")
        effective_ref = window_ref or self.window_ref
        row = copy.deepcopy({"sequence": self.sequence + 1, "previous": self.chain,
                             "events": events or [], "checkpoint": checkpoint,
                             # Keep an immutable accepted-view witness in
                             # each journal row.  A model switch may replace
                             # the head with an empty protocol view, while
                             # portable work-state export still needs the
                             # prior accepted facts.
                             "view": view, "created_at": time.time(), "schema_version": 1})
        if effective_ref is not None:
            row["window_ref"] = effective_ref.to_payload()
        if delivery_commit is not None:
            from codey.runtime.effects.tool_result_delivery import DeliveryCommit

            DeliveryCommit.from_payload(delivery_commit)
            row["delivery_commit"] = copy.deepcopy(delivery_commit)
        chain = digest(row)
        try:
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
                    head_payload: dict[str, Any] = {"schema_version": 1, "identity": self.identity,
                                      "session_id": effective_ref.session_id if effective_ref is not None else "",
                                      "sequence": self.sequence + 1,
                                      "chain": chain, "view": view, "view_digest": digest(view)}
                    if effective_ref is not None:
                        head_payload["window_ref"] = effective_ref.to_payload()
                    write_json_atomic(head_path, head_payload, max_bytes=MAX_SEGMENT_BYTES)
        except Exception:
            self.window_ref = previous_ref
            raise
        self.window_ref = effective_ref
        self.sequence += 1
        self.chain = chain
        self._records.append(row)
        self.view = copy.deepcopy(view)

    def commit_exchange(
        self,
        expected_ref: ProtocolWindowRef,
        view: list[dict[str, Any]],
        *,
        events: list[dict[str, Any]] | None = None,
        checkpoints: list[dict[str, Any]] | None = None,
        delivery_commit: dict[str, Any] | None = None,
    ) -> None:
        if self.window_ref != expected_ref:
            from codey.runtime.core.protocol_window import WindowMismatch

            raise WindowMismatch("context ledger window reference does not match")
        checkpoint = {"kind": "transaction", "segments": checkpoints} if checkpoints else None
        self.commit(view, events=events, checkpoint=checkpoint,
                    window_ref=expected_ref, delivery_commit=delivery_commit)

    def open_window(
        self,
        window_ref: ProtocolWindowRef,
        *,
        seed: list[dict[str, Any]] | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.commit(seed or [], events=events, window_ref=window_ref, allow_window_change=True)

    def delivery_commits(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(row["delivery_commit"]) for row in self._records
                     if isinstance(row.get("delivery_commit"), dict))

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
        """Read verified semantic checkpoints from this session's primary ledger."""
        populated: list[ContextLedger] = [
            ledger for ledger in (self,)
            if ledger.view or ledger.events()
            or any(isinstance(row.get("view"), list) and row.get("view") for row in ledger._records)
        ]
        if not populated:
            return ""
        latest = max(populated, key=lambda ledger: ledger._records[-1].get("created_at", 0))
        parts: list[str] = []
        checkpoints = latest.checkpoint_digests()
        # A model switch intentionally starts an empty protocol view while
        # retaining accepted events in the journal.  Portable work state must
        # read those accepted facts instead of treating the new empty view as
        # an empty task.
        source_view = latest.view
        if not source_view:
            for row in reversed(latest._records):
                candidate = row.get("view")
                if isinstance(candidate, list) and candidate:
                    source_view = candidate
                    break
        source_view = source_view or latest.events()
        retained = [item for index, item in enumerate(source_view)
                    if index >= len(source_view) - 12 or digest(item) in checkpoints]
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
