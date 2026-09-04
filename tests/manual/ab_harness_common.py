"""Shared plumbing for manual A/B harness scripts.

This module owns only the plumbing that live probes kept duplicating: the
journaling provider wrapper, interleaved arm schedules, complete-matrix
checks, atomic JSON persistence, resume payloads with provider identity
guards, and the deterministic fixture search provider with its URL-policy
bypass. Harness-specific scorers, cases, and gates stay in each script.

Manual-layer rules inherited from 0.4.6:

- Production code must never import this module (architecture test).
- Transcript material may only flow into an ABJournalWriter, never back into
  RunTrace / EvidenceLedger / production evidence.
"""

from __future__ import annotations

import json
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from tests.manual.ab_journal import (
    TRANSCRIPT_MODE_ARCHIVE,
    TRANSCRIPT_MODE_DIGEST_ONLY,
    ABJournalWriter,
    TranscriptReplayCache,
    journal_directory_for,
)

MAX_RESULT_BYTES = 1024 * 1024
MAX_RESEARCH_RECORD_SOURCES = 24
MAX_RESEARCH_RECORD_EVIDENCE = 48
MAX_RESEARCH_RECORD_CLAIMS = 32
MAX_RESEARCH_RECORD_ASSUMPTIONS = 16
MAX_RESEARCH_RECORD_RELATIONS = 96
MAX_RESEARCH_RECORD_REFS = 48

_RESEARCH_RECORD_TOP_LEVEL_KEYS = (
    "schema_version",
    "kind",
    "record_id",
    "record_digest",
    "question",
    "answer_status",
    "sources",
    "evidence",
    "claims",
    "assumptions",
    "relations",
    "unsupported_claim_count",
    "run_id",
    "session_id",
    "project_ref",
    "synthesis_id",
    "stop_reason",
)

# Closed failure vocabulary for A/B evidence. Every failed row names ONE of
# these classes so post-hoc summaries never re-guess what a crash meant.
AB_FAILURE_NONE = "none"
AB_FAILURE_PROVIDER = "provider_error"
AB_FAILURE_CODEY = "codey_failure"
AB_FAILURE_ENVIRONMENT = "environment_error"
AB_FAILURE_CLASSES = (
    AB_FAILURE_NONE,
    AB_FAILURE_PROVIDER,
    AB_FAILURE_CODEY,
    AB_FAILURE_ENVIRONMENT,
)

PROVIDER_FAILURE_NONE = "none"
PROVIDER_FAILURE_SEND_ERROR = "provider_send_error"
PROVIDER_FAILURE_NO_REPLY = "provider_no_reply"
PROVIDER_FAILURE_NATIVE_SEARCH_STALL = "native_search_stall"
PROVIDER_FAILURE_WEBPAGE_UI_CHANGED = "webpage_ui_changed"
PROVIDER_FAILURE_UNKNOWN = "unknown"
PROVIDER_FAILURE_CLASSES = (
    PROVIDER_FAILURE_NONE,
    PROVIDER_FAILURE_SEND_ERROR,
    PROVIDER_FAILURE_NO_REPLY,
    PROVIDER_FAILURE_NATIVE_SEARCH_STALL,
    PROVIDER_FAILURE_WEBPAGE_UI_CHANGED,
    PROVIDER_FAILURE_UNKNOWN,
)


def row_has_terminal_failure(row: Mapping[str, Any]) -> bool:
    """Return true when a persisted result row cannot be release evidence."""

    if row.get("error"):
        return True
    return str(row.get("stop_reason") or "").strip().lower() == "error"


@dataclass(frozen=True)
class ArmRunLayout:
    """Stable file layout for one provider/suite/arm result output."""

    output_json: Path
    journal_dir: Path
    manifest_path: Path
    transcript_dir: Path

    @classmethod
    def for_output(cls, output: Path, *, journal_dir: Path | None = None) -> "ArmRunLayout":
        output_path = Path(output)
        trace_dir = Path(journal_dir) if journal_dir is not None else journal_directory_for_output(output_path)
        return cls(
            output_json=output_path,
            journal_dir=trace_dir,
            manifest_path=output_path.with_name(f"{output_path.stem}-manifest.json"),
            transcript_dir=trace_dir / "transcripts",
        )


@dataclass(frozen=True)
class ArmManifest:
    """Human/audit-readable manifest bound to a result JSON."""

    suite: str
    provider: str
    arms: tuple[str, ...]
    cases: tuple[str, ...]
    max_turns: int
    output_json: str
    manifest_path: str
    journal_dir: str
    transcript_mode: str
    transcript_dir: str
    started_at: str
    finished_at: str = ""
    stop_reason: str = ""
    provider_error_class: str = PROVIDER_FAILURE_NONE
    codey_failure_class: str = AB_FAILURE_NONE
    resumed_attempt: bool = False
    attempt_index: int = 1
    git_commit: str = ""
    git_dirty: bool | None = None
    dirty_state: str = "unknown"

    def to_payload(self) -> dict[str, Any]:
        if self.provider_error_class not in PROVIDER_FAILURE_CLASSES:
            raise ValueError(f"unknown provider_error_class: {self.provider_error_class}")
        if self.codey_failure_class not in AB_FAILURE_CLASSES:
            raise ValueError(f"unknown codey_failure_class: {self.codey_failure_class}")
        return {
            "suite": self.suite,
            "provider": self.provider,
            "arms": list(self.arms),
            "cases": list(self.cases),
            "max_turns": max(1, int(self.max_turns)),
            "output_json": self.output_json,
            "manifest_path": self.manifest_path,
            "journal_dir": self.journal_dir,
            "transcript_mode": self.transcript_mode,
            "transcript_dir": self.transcript_dir,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stop_reason": self.stop_reason,
            "provider_error_class": self.provider_error_class,
            "codey_failure_class": self.codey_failure_class,
            "resumed_attempt": bool(self.resumed_attempt),
            "attempt_index": max(1, int(self.attempt_index)),
            "git_commit": self.git_commit,
            "git_dirty": self.git_dirty,
            "dirty_state": self.dirty_state,
        }


# CLI transcript flags -> journal modes; "off" means no journal at all.
TRANSCRIPT_MODE_FLAGS: dict[str, str | None] = {
    "off": None,
    "digest-only": TRANSCRIPT_MODE_DIGEST_ONLY,
    "archive": TRANSCRIPT_MODE_ARCHIVE,
}


class OutputProviderMismatch(ValueError):
    """A result file belongs to another provider; refuse to mix arms into it."""

    def __init__(self, *, path: Path, expected: str, found: str) -> None:
        self.path = path
        self.expected = expected
        self.found = found
        super().__init__(f"{path} was created for provider {found!r}; refusing to reuse it for {expected!r}")


class TracingProvider:
    """Provider wrapper that counts traffic and journals every exchange.

    With a journal, each send is fsync-recorded before the request and the
    full prompt/reply pair is archived immediately after the reply (manual
    layer only). With ``journal=None`` it degrades to a plain counting
    provider, which is what scripted self-tests use.

    Timeouts are true pass-through when neither configured nor provided:
    ``send(text)`` / ``new_chat()`` are called without a timeout kwarg, so
    scripted providers with plain signatures keep working. ``close()``
    forwards only when the wrapped provider actually closes.
    """

    def __init__(
        self,
        provider: Any,
        *,
        journal: ABJournalWriter | None = None,
        case: str = "",
        arm: str = "",
        timeout: float | None = None,
        new_chat_timeout: float | None = None,
    ) -> None:
        self.provider = provider
        self.journal = journal
        self.case = case
        self.arm = arm
        self.timeout = None if timeout is None else max(1.0, float(timeout))
        self.new_chat_timeout = None if new_chat_timeout is None else max(1.0, float(new_chat_timeout))
        self.send_index = 0
        self.reply_count = 0
        self.prompt_chars = 0
        self.reply_chars = 0
        self.prompts: list[str] = []
        self.replies: list[str] = []
        self.transcript_refs: list[dict[str, object]] = []
        self.last_turn = 0
        self.last_reply = ""
        self.name = getattr(provider, "name", "provider")
        self.location = getattr(provider, "location", "")
        self.id = getattr(provider, "id", "")
        self.thread_safe_send = getattr(provider, "thread_safe_send", False)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.provider, name)

    def new_chat(self, timeout: float | None = None) -> object:
        if timeout is not None:
            return self.provider.new_chat(timeout=timeout)
        if self.new_chat_timeout is None:
            return self.provider.new_chat()
        return self.provider.new_chat(timeout=self.new_chat_timeout)

    def send(self, text: str, timeout: float | None = None) -> str:
        prompt = str(text or "")
        self.send_index += 1
        turn = self.send_index
        self.prompt_chars += len(prompt)
        if self.journal is not None:
            self.journal.record_send_start(case=self.case, arm=self.arm, turn=turn, prompt=prompt)
        try:
            reply_text = str(self._send_through(prompt, timeout))
        except Exception as exc:
            if self.journal is not None:
                self.journal.record_send_error(
                    case=self.case,
                    arm=self.arm,
                    turn=turn,
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise
        self.prompts.append(prompt)
        self.replies.append(reply_text)
        self.reply_count += 1
        self.reply_chars += len(reply_text)
        self.last_turn = turn
        self.last_reply = reply_text
        if self.journal is not None:
            event = self.journal.record_reply(case=self.case, arm=self.arm, turn=turn, prompt=prompt, reply=reply_text)
            self.transcript_refs.append(dict(event.content_ref))
        return reply_text

    def _send_through(self, prompt: str, timeout: float | None) -> Any:
        effective = timeout if timeout is not None else self.timeout
        if effective is None:
            return self.provider.send(prompt)
        return self.provider.send(prompt, timeout=effective)

    def close(self) -> None:
        closer = getattr(self.provider, "close", None)
        if callable(closer):
            return closer()
        return None


def interleaved_arm_schedule(arms: Sequence[str], repeats: int) -> list[tuple[str, int]]:
    """Interleave arm order per repeat to cancel warm-session/order bias."""

    arm_names = tuple(arms)
    out: list[tuple[str, int]] = []
    for repeat_index in range(max(1, int(repeats))):
        order = arm_names if repeat_index % 2 == 0 else tuple(reversed(arm_names))
        out.extend((arm, repeat_index + 1) for arm in order)
    return out


def expected_matrix_keys(cases: Sequence[str], repeats: int) -> set[tuple[str, int]]:
    repeat_count = max(1, int(repeats))
    return {(case, index + 1) for case in tuple(cases) for index in range(repeat_count)}


def matrix_complete(
    rows: Sequence[Mapping[str, Any]],
    *,
    arms: Sequence[str],
    cases: Sequence[str],
    repeats: int,
) -> bool:
    """True when every (case, repeat) has exactly one row per arm and no row
    is missing -- a clean crash-free run needs a complete matrix to gate."""

    arm_names = tuple(arms)
    expected = expected_matrix_keys(cases, repeats)
    if len(rows) != len(expected) * len(arm_names):
        return False

    def pair_counts(arm_rows: list[Mapping[str, Any]]) -> dict[tuple[str, int], int]:
        counts: dict[tuple[str, int], int] = {}
        for row in arm_rows:
            key = (str(row.get("case") or ""), int(row.get("repeat") or 0))
            counts[key] = counts.get(key, 0) + 1
        return counts

    for arm in arm_names:
        arm_rows = [row for row in rows if row.get("arm") == arm]
        pairs = pair_counts(arm_rows)
        if any(pairs.get(key, 0) != 1 for key in expected):
            return False
    return True


def bounded_error_row(*, case: str, arm: str, repeat: int, exc: BaseException) -> dict[str, Any]:
    return {
        "case": case,
        "arm": arm,
        "repeat": max(1, int(repeat)),
        "error": f"{type(exc).__name__}: {exc}",
    }


def research_record_payload(record: object) -> dict[str, Any]:
    """Return a bounded ResearchRecord-shaped payload for manual A/B rows."""

    raw: object = {}
    to_jsonable = getattr(record, "to_jsonable", None)
    if callable(to_jsonable):
        try:
            raw = to_jsonable()
        except Exception:
            raw = {}
    elif isinstance(record, Mapping):
        raw = record
    if not isinstance(raw, Mapping):
        return {}
    payload = _sanitize_research_record_payload(raw)
    if not _looks_like_research_record_payload(payload):
        return {}
    return payload


def attach_research_record_payload(row: dict[str, Any], record: object) -> dict[str, Any]:
    """Attach a bounded ResearchRecord payload so projection can locate claims."""

    payload = research_record_payload(record)
    if payload:
        row["research_record"] = payload
        row["research_record_included"] = True
    else:
        row.pop("research_record", None)
        row["research_record_included"] = False
    return row


def _sanitize_research_record_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key in _RESEARCH_RECORD_TOP_LEVEL_KEYS:
        if key not in payload:
            continue
        if key == "question":
            clean[key] = _sanitize_question_ref(payload.get(key))
        elif key == "project_ref":
            clean[key] = _sanitize_project_ref(payload.get(key))
        elif key == "sources":
            clean[key] = _sanitize_record_sequence(
                payload.get(key),
                sanitizer=_sanitize_source_record,
                limit=MAX_RESEARCH_RECORD_SOURCES,
            )
        elif key == "evidence":
            clean[key] = _sanitize_record_sequence(
                payload.get(key),
                sanitizer=_sanitize_evidence_record,
                limit=MAX_RESEARCH_RECORD_EVIDENCE,
            )
        elif key == "claims":
            clean[key] = _sanitize_record_sequence(
                payload.get(key),
                sanitizer=_sanitize_claim_record,
                limit=MAX_RESEARCH_RECORD_CLAIMS,
            )
        elif key == "assumptions":
            clean[key] = _sanitize_record_sequence(
                payload.get(key),
                sanitizer=_sanitize_assumption_record,
                limit=MAX_RESEARCH_RECORD_ASSUMPTIONS,
            )
        elif key == "relations":
            clean[key] = _sanitize_record_sequence(
                payload.get(key),
                sanitizer=_sanitize_relation_record,
                limit=MAX_RESEARCH_RECORD_RELATIONS,
            )
        elif key in {"schema_version", "unsupported_claim_count"}:
            clean[key] = _nonnegative_int(payload.get(key))
        elif key == "kind":
            clean[key] = _clip(payload.get(key), 80) or "research_record"
        elif key == "answer_status":
            clean[key] = _clip(payload.get(key), 40)
        elif key in {"record_id", "run_id", "session_id", "synthesis_id"}:
            clean[key] = _clip(payload.get(key), 120)
        elif key == "record_digest":
            clean[key] = _clip(payload.get(key), 80)
        elif key == "stop_reason":
            clean[key] = _clip(payload.get(key), 80)
    for key in ("sources", "evidence", "claims", "assumptions", "relations"):
        clean.setdefault(key, [])
    if not clean.get("kind") and _looks_like_research_record_payload(clean):
        clean["kind"] = "research_record"
    return clean


def _looks_like_research_record_payload(payload: Mapping[str, Any]) -> bool:
    has_lists = all(
        isinstance(payload.get(key), list)
        for key in ("sources", "evidence", "claims", "assumptions", "relations")
    )
    has_answer_status = bool(str(payload.get("answer_status") or "").strip())
    if payload.get("kind") == "research_record":
        return has_lists and has_answer_status
    claims = payload.get("claims")
    return bool(
        has_lists
        and has_answer_status
        and isinstance(claims, list)
        and any(isinstance(claim, Mapping) and claim.get("claim_id") for claim in claims)
    )


def _sanitize_record_sequence(
    value: object,
    *,
    sanitizer: Callable[[Mapping[str, Any]], dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    rows: list[dict[str, Any]] = []
    max_items = max(0, int(limit))
    for index, item in enumerate(value):
        if index >= max_items:
            break
        if not isinstance(item, Mapping):
            continue
        clean = sanitizer(item)
        if clean:
            rows.append(clean)
    return rows


def _sanitize_question_ref(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        "question_id": _clip(value.get("question_id"), 120),
        "question_text_digest": _clip(value.get("question_text_digest"), 80),
        "chars": _nonnegative_int(value.get("chars")),
    }


def _sanitize_project_ref(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    clean: dict[str, Any] = {}
    if value.get("basename"):
        clean["basename"] = _clip(value.get("basename"), 80)
    if value.get("digest"):
        clean["digest"] = _clip(value.get("digest"), 80)
    return clean


def _sanitize_source_record(value: Mapping[str, Any]) -> dict[str, Any]:
    source_id = _clip(value.get("source_id"), 120)
    if not source_id:
        return {}
    return {
        "source_id": source_id,
        "requested_url_ref": _sanitize_url_ref(value.get("requested_url_ref")),
        "final_url_ref": _sanitize_url_ref(value.get("final_url_ref")),
        "host": _clip(value.get("host"), 120),
        "title_digest": _clip(value.get("title_digest"), 80),
        "content_hash": _clip(value.get("content_hash"), 80),
        "retrieved_at": _clip(value.get("retrieved_at"), 80),
        "content_kind": _clip(value.get("content_kind"), 40) or "html",
        "page_count": _nonnegative_int(value.get("page_count")),
        "pages_read": _positive_int_list(value.get("pages_read"), limit=MAX_RESEARCH_RECORD_REFS),
        "truncated": bool(value.get("truncated")),
        "quality": _sanitize_source_quality(value.get("quality")),
    }


def _sanitize_evidence_record(value: Mapping[str, Any]) -> dict[str, Any]:
    evidence_id = _clip(value.get("evidence_id"), 120)
    source_id = _clip(value.get("source_id"), 120)
    if not evidence_id or not source_id:
        return {}
    return {
        "evidence_id": evidence_id,
        "source_id": source_id,
        "excerpt_digest": _clip(value.get("excerpt_digest"), 80),
        "bounded_excerpt": _clip(value.get("bounded_excerpt"), 360),
        "locator": _sanitize_evidence_locator(value.get("locator")),
        "stance": _clip(value.get("stance"), 40) or "supports",
        "note_id": _clip(value.get("note_id"), 120),
        "claim_text_digest": _clip(value.get("claim_text_digest"), 80),
    }


def _sanitize_claim_record(value: Mapping[str, Any]) -> dict[str, Any]:
    claim_id = _clip(value.get("claim_id"), 120)
    if not claim_id:
        return {}
    return {
        "claim_id": claim_id,
        "claim_text": _clip(value.get("claim_text"), 260),
        "claim_section": _clip(value.get("claim_section"), 80),
        "citation_numbers": _positive_int_list(value.get("citation_numbers"), limit=MAX_RESEARCH_RECORD_REFS),
        "evidence_refs": _bounded_string_list(value.get("evidence_refs"), limit=MAX_RESEARCH_RECORD_REFS),
        "assumption_refs": _bounded_string_list(value.get("assumption_refs"), limit=MAX_RESEARCH_RECORD_REFS),
        "status": _clip(value.get("status"), 40) or "unsupported",
    }


def _sanitize_assumption_record(value: Mapping[str, Any]) -> dict[str, Any]:
    assumption_id = _clip(value.get("assumption_id"), 120)
    if not assumption_id:
        return {}
    return {
        "assumption_id": assumption_id,
        "assumption_text": _clip(value.get("assumption_text"), 260),
        "reason": _clip(value.get("reason"), 80),
        "claim_ref": _clip(value.get("claim_ref"), 120),
    }


def _sanitize_relation_record(value: Mapping[str, Any]) -> dict[str, Any]:
    relation_id = _clip(value.get("relation_id"), 120)
    from_ref = _clip(value.get("from_ref"), 120)
    to_ref = _clip(value.get("to_ref"), 120)
    if not relation_id or not from_ref or not to_ref:
        return {}
    return {
        "relation_id": relation_id,
        "relation_kind": _clip(value.get("relation_kind"), 40),
        "from_ref": from_ref,
        "to_ref": to_ref,
        "citation_numbers": _positive_int_list(value.get("citation_numbers"), limit=MAX_RESEARCH_RECORD_REFS),
    }


def _sanitize_url_ref(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    clean: dict[str, Any] = {}
    for key, limit in (
        ("url_digest", 80),
        ("host", 120),
        ("scheme", 20),
        ("path_digest", 80),
    ):
        if value.get(key):
            clean[key] = _clip(value.get(key), limit)
    if value.get("redacted"):
        clean["redacted"] = True
    return clean


def _sanitize_evidence_locator(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    clean: dict[str, Any] = {
        "kind": _clip(value.get("kind"), 40) or "unknown",
        "source_id": _clip(value.get("source_id"), 120),
        "char_start": _nonnegative_int(value.get("char_start")),
        "char_end": _nonnegative_int(value.get("char_end")),
    }
    page = _nonnegative_int(value.get("page"))
    if page:
        clean["page"] = page
    if value.get("locator"):
        clean["locator"] = _clip(value.get("locator"), 80)
    return clean


def _sanitize_source_quality(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    clean: dict[str, Any] = {}
    for key in ("level", "kind", "freshness", "independent_group"):
        if value.get(key):
            clean[key] = _clip(value.get(key), 40)
    return clean


def _positive_int_list(value: object, *, limit: int) -> list[int]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[int] = []
    max_items = max(0, int(limit))
    for index, item in enumerate(value):
        if index >= max_items:
            break
        if isinstance(item, bool):
            continue
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0:
            out.append(number)
    return out


def _bounded_string_list(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    max_items = max(0, int(limit))
    for index, item in enumerate(value):
        if index >= max_items:
            break
        text = _clip(item, 120)
        if text:
            out.append(text)
    return out


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def write_payload_bounded(path: Path, payload: Mapping[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
        raise ValueError(f"result exceeded bounded size: {path.name}")
    write_json_atomic(path, payload)


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _row_key(row: Mapping[str, Any], *, provider_id: str = "") -> tuple[str, str, str, int]:
    repeat = row.get("repeat")
    if repeat in (None, ""):
        repeat = row.get("sample")
    return (
        str(provider_id or row.get("provider") or "").strip().lower(),
        str(row.get("case") or "").strip(),
        str(row.get("arm") or "").strip(),
        max(1, int(repeat or 1)),
    )


def transcript_path_for_row(
    row: Mapping[str, Any],
    *,
    layout: ArmRunLayout | None = None,
) -> Path | None:
    """Return the archived transcript path referenced by a row, if any."""

    raw_refs = row.get("transcript_refs") or row.get("transcript_ref") or ()
    refs: Iterable[object]
    if isinstance(raw_refs, Mapping):
        refs = (raw_refs,)
    elif isinstance(raw_refs, (list, tuple)):
        refs = raw_refs
    else:
        refs = ()
    for ref in refs:
        if not isinstance(ref, Mapping):
            continue
        raw_path = str(ref.get("path") or "").strip()
        if not raw_path:
            continue
        path = Path(raw_path)
        if path.is_absolute():
            return path
        if layout is not None:
            return layout.journal_dir / path
        return path
    return None


def bind_row_evidence_refs(
    row: dict[str, Any],
    *,
    layout: ArmRunLayout,
    tracing_provider: TracingProvider | None = None,
) -> dict[str, Any]:
    """Attach result/journal/transcript refs without raw transcript content."""

    row["output_json"] = str(layout.output_json)
    row["journal_dir"] = str(layout.journal_dir)
    refs = list(getattr(tracing_provider, "transcript_refs", []) or ())
    if refs:
        row["transcript_refs"] = refs
        row["transcript_replayable"] = bool(refs) and all(
            _transcript_ref_is_replayable(ref, layout=layout) for ref in refs
        )
    return row


def _transcript_ref_is_replayable(ref: object, *, layout: ArmRunLayout) -> bool:
    if not isinstance(ref, Mapping):
        return False
    raw_path = str(ref.get("path") or "").strip()
    if not raw_path:
        return False
    path = Path(raw_path)
    if not path.is_absolute():
        path = layout.journal_dir / path
    return path.is_file()


def upsert_case_row(
    rows: list[dict[str, Any]],
    row: Mapping[str, Any],
    *,
    provider_id: str = "",
) -> None:
    """Replace an existing provider/case/arm/repeat row, then append new row."""

    replacement = dict(row)
    key = _row_key(replacement, provider_id=provider_id)
    if not key[1] or not key[2]:
        raise ValueError("row must include non-empty case and arm")
    rows[:] = [existing for existing in rows if _row_key(existing, provider_id=provider_id) != key]
    rows.append(replacement)


def summarize_arm_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Generic summary for scripts without a richer domain scorer."""

    by_arm: dict[str, dict[str, Any]] = {}
    for row in rows:
        arm = str(row.get("arm") or "").strip() or "<unknown>"
        item = by_arm.setdefault(
            arm,
            {
                "total": 0,
                "ok": 0,
                "errors": 0,
                "provider_failures": 0,
                "codey_failures": 0,
                "transcript_replayable": 0,
            },
        )
        item["total"] += 1
        if row.get("error"):
            item["errors"] += 1
        if row.get("ok") or row.get("success") or row.get("exact"):
            item["ok"] += 1
        provider_failure = row.get("provider_failure_class") or row.get("provider_error_class")
        if str(provider_failure or "") not in ("", AB_FAILURE_NONE):
            item["provider_failures"] += 1
        if str(row.get("codey_failure_class") or "") not in ("", AB_FAILURE_NONE):
            item["codey_failures"] += 1
        if row.get("transcript_replayable"):
            item["transcript_replayable"] += 1
    return {
        "rows": len(rows),
        "errors": sum(1 for row in rows if row.get("error")),
        "by_arm": by_arm,
    }


def merge_unique_names(*values: object) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            candidates: tuple[object, ...] = (value,)
        else:
            try:
                candidates = tuple(value)  # type: ignore[arg-type]
            except TypeError:
                candidates = (value,)
        for candidate in candidates:
            name = str(candidate or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            merged.append(name)
    return merged


def case_names(cases: Iterable[object]) -> list[str]:
    """Accept Case objects, plain name strings, or lists of either."""

    out: list[str] = []
    for case in cases or ():
        if isinstance(case, str):
            out.append(case)
            continue
        try:
            nested = tuple(case)  # type: ignore[arg-type]
        except TypeError:
            nested = (case,)
        for item in nested:
            out.append(str(getattr(item, "name", "") or item))
    return out


def new_payload(probe: str, *, provider_id: str, cases: Iterable[object], arms: Sequence[str]) -> dict[str, Any]:
    return {
        "probe": probe,
        "provider": provider_id,
        "started_at": timestamp(),
        "updated_at": timestamp(),
        "trace_output": "",
        "cases": case_names(cases),
        "arms": list(arms),
        "complete": False,
        "rows": [],
        "summary": {},
    }


def ensure_output_provider_identity(payload: Mapping[str, Any], *, provider_id: str, output: Path) -> None:
    found = str(payload.get("provider") or "").strip().lower()
    expected = str(provider_id or "").strip().lower()
    if found and expected and found != expected:
        raise OutputProviderMismatch(path=output, expected=expected, found=found)


def normalize_payload_metadata(
    payload: dict[str, Any],
    *,
    provider_id: str,
    cases: Iterable[object],
    arms: Sequence[str],
) -> None:
    payload["provider"] = provider_id
    payload["cases"] = merge_unique_names(payload.get("cases"), case_names(cases))
    payload["arms"] = merge_unique_names(payload.get("arms"), list(arms))


def load_or_new_payload(
    output: Path,
    *,
    probe: str,
    provider_id: str,
    cases: Iterable[object],
    arms: Sequence[str],
) -> dict[str, Any]:
    """Resume an interrupted run's rows, or start a fresh payload."""

    if output.exists():
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
                ensure_output_provider_identity(payload, provider_id=provider_id, output=output)
                payload["complete"] = False
                return payload
        except (OSError, json.JSONDecodeError):
            pass
    return new_payload(probe, provider_id=provider_id, cases=cases, arms=arms)


class ResultRowStore:
    """Resume-safe result JSON writer for one fixed manual A/B output.

    The store never removes rows on disk while deciding what is pending. A
    replacement row is first assembled in memory and only becomes visible via
    the final atomic write, so a provider connection failure before the new row
    exists leaves the previous report intact.
    """

    def __init__(
        self,
        output: Path,
        payload: dict[str, Any],
        *,
        provider_id: str,
        summarize: Callable[[list[dict[str, Any]]], Mapping[str, Any]] | None = None,
        ok: Callable[[list[dict[str, Any]], bool], bool] | None = None,
    ) -> None:
        self.output = Path(output)
        self.payload = payload
        self.provider_id = str(provider_id or "").strip().lower()
        self.summarize = summarize or summarize_arm_rows
        self.ok = ok
        rows = self.payload.get("rows")
        if not isinstance(rows, list):
            rows = []
        self.payload["rows"] = [dict(row) for row in rows if isinstance(row, Mapping)]

    @classmethod
    def open(
        cls,
        output: Path,
        *,
        probe: str,
        provider_id: str,
        cases: Iterable[object],
        arms: Sequence[str],
        summarize: Callable[[list[dict[str, Any]]], Mapping[str, Any]] | None = None,
        ok: Callable[[list[dict[str, Any]], bool], bool] | None = None,
    ) -> "ResultRowStore":
        payload = load_or_new_payload(
            output,
            probe=probe,
            provider_id=provider_id,
            cases=cases,
            arms=arms,
        )
        normalize_payload_metadata(payload, provider_id=provider_id, cases=cases, arms=arms)
        return cls(output, payload, provider_id=provider_id, summarize=summarize, ok=ok)

    @property
    def rows(self) -> list[dict[str, Any]]:
        return self.payload["rows"]

    def existing_keys(self, *, rerun_failed: bool = False) -> set[tuple[str, str, int]]:
        keys: set[tuple[str, str, int]] = set()
        for row in self.rows:
            if rerun_failed and row_has_terminal_failure(row):
                continue
            _provider, case, arm, repeat = _row_key(row, provider_id=self.provider_id)
            keys.add((case, arm, repeat))
        return keys

    def pending_keys(
        self,
        *,
        cases: Iterable[object],
        arms: Sequence[str],
        repeats: int = 1,
        rerun_failed: bool = False,
    ) -> list[tuple[str, str, int]]:
        existing = self.existing_keys(rerun_failed=rerun_failed)
        names = case_names(cases)
        repeat_count = max(1, int(repeats))
        return [
            (case, arm, repeat)
            for case in names
            for repeat in range(1, repeat_count + 1)
            for arm in arms
            if (case, str(arm), repeat) not in existing
        ]

    def upsert(
        self,
        row: Mapping[str, Any],
        *,
        complete: bool = False,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        upsert_case_row(self.rows, row, provider_id=self.provider_id)
        self.write(complete=complete, extra=extra)

    def write(
        self,
        *,
        complete: bool = False,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        self.payload["summary"] = dict(self.summarize(self.rows))
        self.payload["ok"] = (
            bool(self.ok(self.rows, bool(complete)))
            if self.ok is not None
            else not any(row_has_terminal_failure(row) for row in self.rows)
        )
        self.payload["complete"] = bool(complete)
        self.payload["updated_at"] = timestamp()
        if extra:
            self.payload.update(dict(extra))
        write_payload_bounded(self.output, self.payload)


def journal_directory_for_output(output: Path) -> Path:
    return journal_directory_for(output)


def git_state(repo: Path | None = None) -> dict[str, Any]:
    """Commit + dirty flag for A/B evidence, failing soft to unknowns."""

    state: dict[str, Any] = {"git_commit": "", "git_dirty": None}
    try:
        commit_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo) if repo else None,
            capture_output=True,
            text=False,
            timeout=10,
            check=True,
        )
        status_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo) if repo else None,
            capture_output=True,
            text=False,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return state
    state["git_commit"] = bytes(commit_proc.stdout or b"").decode("ascii", "replace").strip()
    state["git_dirty"] = bool(bytes(status_proc.stdout or b"").strip())
    return state


def build_arm_manifest(
    *,
    suite: str,
    provider: str,
    arms: Sequence[str],
    cases: Sequence[str],
    max_turns: int,
    journal_dir: Path | None,
    transcript_mode: str,
    started_at: str,
    finished_at: str = "",
    stop_reason: str = "",
    provider_error_class: str = PROVIDER_FAILURE_NONE,
    codey_failure_class: str = AB_FAILURE_NONE,
    resumed_attempt: bool = False,
    attempt_index: int = 1,
    repo: Path | None = None,
) -> dict[str, Any]:
    """The fixed per-arm evidence schema every manual A/B must persist."""

    state = git_state(repo)
    dirty = state.get("git_dirty")
    dirty_state = "unknown" if dirty is None else "dirty" if dirty else "clean"
    return ArmManifest(
        suite=str(suite or "").strip(),
        provider=str(provider or "").strip(),
        arms=tuple(str(arm) for arm in arms),
        cases=tuple(str(case) for case in cases),
        max_turns=max(1, int(max_turns)),
        output_json="",
        manifest_path="",
        journal_dir=str(journal_dir) if journal_dir is not None else "",
        transcript_mode=str(transcript_mode or "off"),
        transcript_dir="",
        started_at=started_at,
        finished_at=finished_at,
        stop_reason=stop_reason,
        provider_error_class=provider_error_class,
        codey_failure_class=codey_failure_class,
        resumed_attempt=bool(resumed_attempt),
        attempt_index=max(1, int(attempt_index)),
        git_commit=str(state.get("git_commit") or ""),
        git_dirty=dirty if isinstance(dirty, bool) else None,
        dirty_state=dirty_state,
    ).to_payload()


def write_arm_manifest(output: Path, manifest: Mapping[str, Any]) -> Path:
    """Persist the arm manifest next to its result JSON, atomically."""

    layout = ArmRunLayout.for_output(
        output,
        journal_dir=Path(str(manifest.get("journal_dir") or ""))
        if str(manifest.get("journal_dir") or "").strip()
        else None,
    )
    path = layout.manifest_path
    payload = dict(manifest)
    payload["output_json"] = str(layout.output_json)
    payload["manifest_path"] = str(path)
    if payload.get("journal_dir"):
        payload["journal_dir"] = str(layout.journal_dir)
        payload["transcript_dir"] = str(layout.transcript_dir)
    else:
        payload["transcript_dir"] = ""
    write_json_atomic(path, payload)
    return path


def open_journal_for_output(
    *,
    output: Path,
    experiment_id: str,
    provider_id: str,
    transcript_mode: str,
    case_names: Sequence[str],
    arms: Sequence[str],
    max_turns: int,
    run_id: str = "",
    journal_dir: Path | None = None,
) -> ABJournalWriter | None:
    """Shared journal opener: None for "off", otherwise a run-start writer.

    Journal identity is derived from the output path so resume runs append
    to the same event chain instead of forking a new one.
    """

    mode = TRANSCRIPT_MODE_FLAGS.get(str(transcript_mode or "").strip())
    if mode is None and str(transcript_mode or "").strip() != "off":
        # Unknown flag: fail closed rather than silently archiving nothing.
        raise ValueError(f"unknown transcript mode: {transcript_mode}")
    if mode is None:
        return None
    layout = ArmRunLayout.for_output(output, journal_dir=journal_dir)
    resolved_dir = layout.journal_dir
    writer = ABJournalWriter(
        directory=resolved_dir,
        experiment_id=experiment_id,
        run_id=run_id or f"{provider_id}-{output.stem}",
        provider=provider_id,
        transcript_cache=TranscriptReplayCache(resolved_dir, mode=mode),
    )
    writer.record_run_start(
        cases=tuple(case_names),
        arms=tuple(arms),
        max_turns=max(4, int(max_turns)),
        resumed_attempt=writer.event_count > 0,
    )
    return writer


def append_or_replace_failed_row(
    rows: list[dict[str, Any]],
    row: dict[str, Any],
    *,
    rerun_failed: bool,
) -> None:
    """Append a row; with rerun_failed, drop prior error rows for the pair."""

    if rerun_failed:
        upsert_case_row(rows, row)
        return
    rows.append(dict(row))


def classify_provider_failure(
    *,
    sends: int = 0,
    replies: int = 0,
    error: object = "",
    stage: str = "",
) -> str:
    """Manual A/B provider failure classifier with closed reason codes."""

    text = f"{type(error).__name__ if isinstance(error, BaseException) else ''} {error} {stage}".lower()
    if any(token in text for token in ("selector", "locator", "element", "not visible", "strict mode")):
        return PROVIDER_FAILURE_WEBPAGE_UI_CHANGED
    if any(token in text for token in ("timeout", "timed out")) and int(sends or 0) > int(replies or 0):
        return PROVIDER_FAILURE_NATIVE_SEARCH_STALL
    if any(token in text for token in ("connection", "socket", "http", "send", "browser closed", "target closed")):
        return PROVIDER_FAILURE_SEND_ERROR
    if int(sends or 0) > 0 and int(replies or 0) <= 0:
        return PROVIDER_FAILURE_NO_REPLY
    if text.strip():
        return PROVIDER_FAILURE_UNKNOWN
    return PROVIDER_FAILURE_NONE


def classify_ab_failure(exc: BaseException) -> str:
    """Coarse first-pass classification for crashed (case, arm) cells."""

    provider_failure = classify_provider_failure(error=exc)
    if provider_failure not in (PROVIDER_FAILURE_NONE, PROVIDER_FAILURE_UNKNOWN):
        return AB_FAILURE_PROVIDER
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(token in text for token in ("permissionerror", "nospaceleft", "os error")):
        return AB_FAILURE_ENVIRONMENT
    return AB_FAILURE_CODEY


@dataclass(frozen=True)
class FixtureDocument:
    url: str
    title: str
    text: str
    keywords: tuple[str, ...] = ()
    default: bool = False

    def result(self) -> dict[str, str]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": _clip(self.text, 260),
        }


class FixtureSearchProvider:
    """Deterministic offline search provider over staged fixture documents."""

    name = "fixture-search"

    def __init__(self, documents: Sequence[FixtureDocument]) -> None:
        self.documents = tuple(documents)
        self.queries: list[str] = []
        self.fetches: list[str] = []
        self.material_phase = False

    def search(self, query: str, limit: int = 8) -> list[dict[str, str]]:
        self.queries.append(str(query or ""))
        lower = str(query or "").casefold()
        if self.material_phase:
            matches = [
                doc
                for doc in self.documents
                if doc.keywords and any(keyword.casefold() in lower for keyword in doc.keywords)
            ]
        else:
            matches = []
        defaults = [doc for doc in self.documents if doc.default]
        ordered: list[FixtureDocument] = []
        for doc in [*matches, *defaults]:
            if doc not in ordered:
                ordered.append(doc)
        return [doc.result() for doc in ordered[: max(0, int(limit or 0))]]

    def fetch(self, url: str) -> dict[str, object]:
        self.fetches.append(str(url or ""))
        for doc in self.documents:
            if doc.url == url:
                return {
                    "url": doc.url,
                    "title": doc.title,
                    "text": doc.text,
                    "truncated": False,
                }
        return {
            "url": url,
            "title": "",
            "text": "ERROR: fixture URL not found",
            "truncated": False,
        }


@contextmanager
def fixture_material_phase(search: object) -> Iterator[None]:
    """Treat already-opened fixture URLs as fresh material for one search."""

    if not isinstance(search, FixtureSearchProvider):
        yield
        return
    previous = bool(search.material_phase)
    search.material_phase = True
    try:
        yield
    finally:
        search.material_phase = previous


@contextmanager
def fixture_network_policy_bypass(
    allowed_prefixes: Sequence[str] = ("https://source-a.test/", "https://source-b.test/"),
) -> Iterator[None]:
    """Allow *.test fixture hosts through the network policy, scoped."""

    from codey.research import plan_executor as research_plan_executor_module
    from codey.research import tools as research_tools_module
    from codey.policies import network as network_module

    original = (
        network_module.check_fetch_url,
        research_tools_module.check_fetch_url,
        research_plan_executor_module.check_fetch_url,
    )

    def _allow_fixture_urls(url: str, *, resolve: bool = True, use_cache: bool = False) -> str | None:
        text = str(url or "").strip().lower()
        if text.startswith(tuple(allowed_prefixes)):
            return None
        return original[0](url, resolve=resolve, use_cache=use_cache)

    network_module.check_fetch_url = _allow_fixture_urls
    research_tools_module.check_fetch_url = _allow_fixture_urls
    research_plan_executor_module.check_fetch_url = _allow_fixture_urls
    try:
        yield
    finally:
        (
            network_module.check_fetch_url,
            research_tools_module.check_fetch_url,
            research_plan_executor_module.check_fetch_url,
        ) = original


def _clip(value: object, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if limit <= 3:
        return text[:limit]
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


__all__ = [
    "MAX_RESULT_BYTES",
    "AB_FAILURE_CLASSES",
    "AB_FAILURE_CODEY",
    "AB_FAILURE_ENVIRONMENT",
    "AB_FAILURE_NONE",
    "AB_FAILURE_PROVIDER",
    "ArmManifest",
    "ArmRunLayout",
    "FixtureDocument",
    "FixtureSearchProvider",
    "OutputProviderMismatch",
    "PROVIDER_FAILURE_CLASSES",
    "PROVIDER_FAILURE_NATIVE_SEARCH_STALL",
    "PROVIDER_FAILURE_NONE",
    "PROVIDER_FAILURE_NO_REPLY",
    "PROVIDER_FAILURE_SEND_ERROR",
    "PROVIDER_FAILURE_UNKNOWN",
    "PROVIDER_FAILURE_WEBPAGE_UI_CHANGED",
    "ResultRowStore",
    "TRANSCRIPT_MODE_FLAGS",
    "TracingProvider",
    "append_or_replace_failed_row",
    "attach_research_record_payload",
    "bind_row_evidence_refs",
    "bounded_error_row",
    "build_arm_manifest",
    "case_names",
    "classify_ab_failure",
    "classify_provider_failure",
    "expected_matrix_keys",
    "fixture_material_phase",
    "fixture_network_policy_bypass",
    "git_state",
    "interleaved_arm_schedule",
    "journal_directory_for_output",
    "load_or_new_payload",
    "matrix_complete",
    "merge_unique_names",
    "new_payload",
    "normalize_payload_metadata",
    "open_journal_for_output",
    "research_record_payload",
    "row_has_terminal_failure",
    "transcript_path_for_row",
    "timestamp",
    "upsert_case_row",
    "write_arm_manifest",
    "write_json_atomic",
    "write_payload_bounded",
]
