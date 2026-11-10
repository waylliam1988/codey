"""Typed display handles derived from the existing task projections.

Handles are aliases for facts already owned by the task session or research
ledger.  This module keeps no mapping table and never copies source text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from codey.operations.task_session import TaskSession
from codey.research.ledger import EvidenceItem, OpenedSource, ResearchLedger
from codey.runtime.core.models import ToolResult
from codey.runtime.effects.effect_records import compute_args_digest
from codey.toolchain.tool_spec import spec_for_tool

_HANDLE_RE = re.compile(
    r"^(?P<prefix>rc|r|s|e)(?P<number>[1-9][0-9]*)(?P<version>@[0-9a-f]{16})?$"
)
HandleKind = Literal["receipt", "search_result", "source", "evidence"]


class ReferenceHandleError(ValueError):
    """A display handle is absent, stale, or outside the current task scope."""


@dataclass(frozen=True)
class ResolvedReference:
    handle: str
    kind: HandleKind
    ref: str
    tool_name: str = ""
    args_digest: str = ""
    url: str = ""
    source_ref: str = ""
    version: str = ""
    result: ToolResult | None = None
    source: OpenedSource | None = None
    evidence: EvidenceItem | None = None


class ReferenceHandleResolver:
    """Resolve short names from one live task projection."""

    def __init__(self, session: TaskSession, *, ledger: ResearchLedger | None = None) -> None:
        self._session = session
        self._ledger = ledger

    def resolve(self, handle: str) -> ResolvedReference:
        text = str(handle or "").strip().lower()
        match = _HANDLE_RE.fullmatch(text)
        if match is None:
            raise ReferenceHandleError("invalid reference handle")
        prefix = match.group("prefix")
        number = int(match.group("number"))
        version = str(match.group("version") or "").removeprefix("@")
        if prefix == "rc":
            if version:
                raise ReferenceHandleError("receipt handles cannot carry a source version")
            return self._receipt(text, number)
        if prefix == "r":
            if version:
                raise ReferenceHandleError("search result handles cannot carry a source version")
            return self._search_result(text, number)
        if prefix == "s":
            return self._source(text, number, version)
        return self._evidence(text, number, version)

    def handle_for(self, ref: str) -> str:
        """Create the short, version-bound display form for an existing ref."""
        text = str(ref or "").strip().lower()
        if text in self._session._memory_results:
            rows = self._receipt_rows()
            return f"rc{rows.index(text) + 1}"
        if text.startswith(("rc", "r")):
            self.resolve(text)
            return text
        if text.startswith("s"):
            resolved = self._source(text, self._number(text, "s"), "")
            return f"{resolved.ref}@{resolved.version}"
        if text.startswith("e"):
            resolved = self._evidence(text, self._number(text, "e"), "")
            return f"{resolved.ref}@{resolved.version}"
        raise ReferenceHandleError("invalid reference identity")

    def _receipt(self, handle: str, number: int) -> ResolvedReference:
        rows = tuple((ref, self._session._memory_results[ref]) for ref in self._receipt_rows())
        if number > len(rows):
            raise ReferenceHandleError("receipt handle is unavailable in this task")
        ref, result = rows[number - 1]
        spec = spec_for_tool(result.call.name)
        if spec is None or not self._session.policy.allows(spec.grant):
            raise ReferenceHandleError("receipt capability is no longer authorized")
        return ResolvedReference(
            handle=handle,
            kind="receipt",
            ref=ref,
            tool_name=result.call.name,
            args_digest=compute_args_digest(result.call.args),
            result=result,
        )

    def _receipt_rows(self) -> tuple[str, ...]:
        """Order receipts from stable call identity and parent-child facts.

        A nested child settles before its parent in a live projection, while
        durable recovery rebuilds intents in parent-first order.  Replaying
        the committed child references before their parent preserves the
        original display order without depending on dictionary insertion.
        """
        available = set(self._session._memory_results)
        records = self._session.executed
        children: dict[str, tuple[str, ...]] = {}
        nested: set[str] = set()
        for parent, record in records.items():
            refs = tuple(ref for ref in record.get("child_effect_ids", ()) if ref in available)
            if refs:
                children[parent] = refs
                nested.update(refs)

        def stable_key(ref: str) -> tuple[str, str, str]:
            result = self._session._memory_results[ref]
            return (result.call.call_id, result.call.name, ref)

        ordered: list[str] = []
        visiting: set[str] = set()

        def visit(ref: str) -> None:
            if ref in visiting or ref not in available:
                return
            visiting.add(ref)
            for child in children.get(ref, ()):
                visit(child)
            ordered.append(ref)

        for ref in sorted(available - nested, key=stable_key):
            visit(ref)
        for ref in sorted(available - set(ordered), key=stable_key):
            visit(ref)
        return tuple(ordered)

    def _search_result(self, handle: str, number: int) -> ResolvedReference:
        ref = f"r{number}"
        url = self._session.search_results.get(ref)
        if url is None or not self._web_allowed():
            raise ReferenceHandleError("search result handle is unavailable")
        return ResolvedReference(handle=handle, kind="search_result", ref=ref, url=url)

    def _source(self, handle: str, number: int, expected_version: str) -> ResolvedReference:
        ref = f"s{number}"
        url = self._session.source_ids.get(ref)
        if url is None or not self._web_allowed():
            raise ReferenceHandleError("source handle is unavailable")
        source = self._find_source(url)
        if source is None:
            raise ReferenceHandleError("source snapshot is unavailable")
        if expected_version and source.text_hash != expected_version:
            raise ReferenceHandleError("source handle refers to a different source version")
        return ResolvedReference(
            handle=handle, kind="source", ref=ref, url=url,
            version=source.text_hash, source=source,
        )

    def _evidence(self, handle: str, number: int, expected_version: str) -> ResolvedReference:
        ledger = self._ledger
        if ledger is None or not self._web_allowed():
            raise ReferenceHandleError("evidence handle is unavailable")
        from codey.research.answer_contract import answer_scope

        scope = answer_scope(self._session)
        aliases = dict(scope.aliases)
        passage_ref = aliases.get(f"e{number}")
        passage = ledger.read_passages.get(passage_ref) if passage_ref else None
        source: OpenedSource | None
        if passage is not None:
            source = passage.source
            item = next(
                (candidate for candidate in ledger.evidence_items
                 if candidate.locator.removeprefix("passage:") == passage.ref),
                EvidenceItem(claim="", source_url=source.final_url, excerpt=passage.excerpt,
                             locator=f"passage:{passage.ref}"),
            )
        else:
            if number > len(ledger.evidence_items):
                raise ReferenceHandleError("evidence handle is unavailable")
            item = ledger.evidence_items[number - 1]
            source = self._find_source(item.source_url)
        if source is None:
            raise ReferenceHandleError("evidence source version is unavailable")
        if expected_version and source.text_hash != expected_version:
            raise ReferenceHandleError("evidence handle refers to a different source version")
        source_ref = next(
            (ref for ref, url in self._session.source_ids.items()
             if url in {source.requested_url, source.final_url, item.source_url}),
            "",
        )
        if not source_ref:
            raise ReferenceHandleError("evidence source is outside this task")
        text = passage.source_text if passage is not None else ledger.source_text_for_url(source.final_url)
        if not item.excerpt or item.excerpt not in text:
            raise ReferenceHandleError("evidence excerpt is not in the source snapshot")
        return ResolvedReference(
            handle=handle, kind="evidence", ref=f"e{number}",
            url=item.source_url, source_ref=source_ref, version=source.text_hash,
            source=source, evidence=item,
        )

    def _find_source(self, url: str) -> OpenedSource | None:
        ledger = self._ledger
        if ledger is None:
            return None
        return next(
            (source for source in reversed(ledger.opened_sources)
             if url in {source.requested_url, source.final_url}),
            None,
        )

    def _web_allowed(self) -> bool:
        return self._session.policy.allows("web.read")

    @staticmethod
    def _number(handle: str, prefix: str) -> int:
        match = _HANDLE_RE.fullmatch(handle)
        if match is None or match.group("prefix") != prefix:
            raise ReferenceHandleError("invalid reference identity")
        return int(match.group("number"))


__all__ = ["ReferenceHandleError", "ReferenceHandleResolver", "ResolvedReference"]
