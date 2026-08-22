"""Durable observation journal shared by manual A/B harnesses.

This is the manual-experiment fact layer, not production RunTrace and not an
Evidence Ledger. It gives every live harness one shape for:

- identity-safe manifests (one experiment/run/provider per directory),
- append-only JSONL events with a verifiable hash chain,
- tail recovery after interrupted runs,
- optional prompt/reply transcript archival behind content digests,
- typed provider-observation facts without DOM, cookies, or raw bodies.

Production layers must not import this module; see test_architecture.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from codey.local_store import write_json_atomic

JOURNAL_SCHEMA_VERSION = 1
MANIFEST_KIND = "ab_observation_journal_manifest"
GENESIS_DIGEST = "sha256:" + "0" * 64

TRANSCRIPT_MODE_DIGEST_ONLY = "digest_only"
TRANSCRIPT_MODE_ARCHIVE = "archive"
TRANSCRIPT_MODES = (TRANSCRIPT_MODE_DIGEST_ONLY, TRANSCRIPT_MODE_ARCHIVE)

EVENT_TYPES = (
    "run_start",
    "case_start",
    "send_start",
    "reply",
    "send_error",
    "timeout",
    "adapter_failure",
    "provider_mismatch",
    "case_complete",
    "run_complete",
    "note",
)

MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024
MAX_FACT_VALUE_CHARS = 200
MAX_FACT_ITEMS = 8
MAX_FACTS_BYTES = 8 * 1024

_URL_RE = re.compile(r"(?i)^[a-z][a-z0-9+.-]*://|^www\.")
_HTML_RE = re.compile(r"(?i)<\s*(html|body|div|script|textarea|input)\b")
_SENSITIVE_KEY_RE = re.compile(r"(?i)(cookie|\bdom\b|html|webpage|page_body|raw_body|stdout|stderr)")
_FACT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,48}$")
_SECRET_VALUE_RE = re.compile(
    r"(?i)(api[-_]?key|authorization|bearer\s|passwd|password|secret)"
)
_SECRET_TOKEN_SHAPE_RE = re.compile(r"\b(?:sk|ghp|github_pat)-?[A-Za-z0-9_-]{16,}\b")


class ABJournalIdentityMismatch(ValueError):
    """Raised when a journal directory belongs to another experiment/run."""


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _digest_payload(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def digest_text(value: object) -> str:
    """Stable sha256 ref for one prompt or reply string."""

    return _digest_payload(str(value or ""))


def journal_directory_for(output: Path) -> Path:
    """Journal directory sits next to the result JSON as ``<stem>.trace/``."""

    return output.with_name(f"{output.stem}.trace")


def looks_secret_shaped(value: str) -> bool:
    return bool(_SECRET_VALUE_RE.search(value) or _SECRET_TOKEN_SHAPE_RE.search(value))


def sanitize_fact_value(value: object) -> tuple[bool, object]:
    """Allow-list one observation fact value.

    Booleans, bounded ints/floats, short clean strings, and flat lists of those
    survive. URLs, HTML fragments, cookie-ish strings, and secret-shaped values
    are redacted instead of persisted.
    """

    if isinstance(value, bool):
        return True, value
    if isinstance(value, int):
        return True, max(-(10**15), min(10**15, value))
    if isinstance(value, float):
        return True, round(value, 6)
    if isinstance(value, str):
        text = " ".join(str(value or "").split())[:MAX_FACT_VALUE_CHARS]
        if not text:
            return False, None
        if _URL_RE.search(text) or _HTML_RE.search(text) or "cookie" in text.lower():
            return True, "[redacted]"
        if looks_secret_shaped(text):
            return True, "[redacted]"
        return True, text
    if isinstance(value, (list, tuple)):
        items: list[object] = []
        for item in list(value)[:MAX_FACT_ITEMS]:
            ok, cleaned = sanitize_fact_value(item)
            if ok:
                items.append(cleaned)
        return True, items
    return False, None


def sanitize_facts(facts: Mapping[str, object] | None) -> dict[str, object]:
    """Project raw provider observations into bounded, safe facts.

    Keys must be lowercase snake_case tokens; cookie/DOM/html/webpage-style keys
    are dropped outright so provider page state can never leak through.
    """

    if not isinstance(facts, Mapping):
        return {}
    cleaned: dict[str, object] = {}
    encoded_size = 0
    for key in sorted(facts, key=str):
        name = str(key or "").strip()
        if not _FACT_KEY_RE.fullmatch(name) or _SENSITIVE_KEY_RE.search(name):
            continue
        allowed, value = sanitize_fact_value(facts[key])
        if not allowed:
            continue
        candidate = json.dumps({name: value}, ensure_ascii=False, default=str)
        size = len(candidate.encode("utf-8"))
        if encoded_size + size > MAX_FACTS_BYTES:
            break
        encoded_size += size
        cleaned[name] = value
    return cleaned


@dataclass(frozen=True)
class TranscriptRef:
    mode: str
    prompt_chars: int
    reply_chars: int
    content_digest: str
    path: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "prompt_chars": self.prompt_chars,
            "reply_chars": self.reply_chars,
            "content_digest": self.content_digest,
            "path": self.path,
        }


class TranscriptReplayCache:
    """Digest-first prompt/reply store for manual replay and scoring.

    ``digest_only`` never writes raw content. ``archive`` stores one bounded
    JSON document per unique prompt/reply pair under ``transcripts/<digest>.json``
    and is idempotent per content digest. Archived transcripts are manual-layer
    material only: they must never flow into RunTrace, EvidenceLedger,
    ResearchRecord, citations, or completion proofs.
    """

    def __init__(
        self,
        directory: Path,
        *,
        mode: str = TRANSCRIPT_MODE_DIGEST_ONLY,
        max_bytes: int = MAX_TRANSCRIPT_BYTES,
    ) -> None:
        if mode not in TRANSCRIPT_MODES:
            raise ValueError(f"unknown transcript mode: {mode}")
        self.directory = Path(directory)
        self.mode = mode
        self.max_bytes = max_bytes

    def ref_for(self, *, prompt: str, reply: str, archive: bool) -> TranscriptRef:
        prompt_text = str(prompt or "")
        reply_text = str(reply or "")
        digest = _digest_payload({"prompt": prompt_text, "reply": reply_text})
        relative = ""
        if archive and self.mode == TRANSCRIPT_MODE_ARCHIVE:
            target = self._transcript_path(digest)
            encoded = json.dumps(
                {
                    "schema_version": JOURNAL_SCHEMA_VERSION,
                    "kind": "ab_transcript",
                    "content_digest": digest,
                    "prompt": prompt_text,
                    "reply": reply_text,
                    "created_at": _timestamp(),
                },
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            if len(encoded) > self.max_bytes:
                raise ValueError("transcript exceeded bounded size")
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
                with temporary.open("wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            relative = f"transcripts/{target.name}"
        return TranscriptRef(
            mode=self.mode if archive else TRANSCRIPT_MODE_DIGEST_ONLY,
            prompt_chars=len(prompt_text),
            reply_chars=len(reply_text),
            content_digest=digest,
            path=relative,
        )

    def _transcript_path(self, digest: str) -> Path:
        return self.directory / "transcripts" / f"{digest.removeprefix('sha256:')}.json"


@dataclass(frozen=True)
class ABJournalEvent:
    seq: int
    ts: str
    run_id: str
    experiment_id: str
    case_id: str
    arm: str
    provider: str
    model: str
    event_type: str
    stage: str
    prompt_digest: str
    reply_digest: str
    content_ref: dict[str, object]
    failure_kind: str
    facts: dict[str, object]
    previous_digest: str
    event_digest: str

    def to_payload(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "run_id": self.run_id,
            "experiment_id": self.experiment_id,
            "case_id": self.case_id,
            "arm": self.arm,
            "provider": self.provider,
            "model": self.model,
            "event_type": self.event_type,
            "stage": self.stage,
            "prompt_digest": self.prompt_digest,
            "reply_digest": self.reply_digest,
            "content_ref": dict(self.content_ref),
            "failure_kind": self.failure_kind,
            "facts": dict(self.facts),
            "previous_digest": self.previous_digest,
            "event_digest": self.event_digest,
        }


_CHAIN_KEYS = (
    "seq",
    "ts",
    "run_id",
    "experiment_id",
    "case_id",
    "arm",
    "provider",
    "model",
    "event_type",
    "stage",
    "prompt_digest",
    "reply_digest",
    "content_ref",
    "failure_kind",
    "facts",
    "previous_digest",
)


def event_chain_digest(payload: Mapping[str, object]) -> str:
    """Recompute the hash-chain digest over the canonical event fields."""

    fields = {key: payload.get(key) for key in _CHAIN_KEYS}
    return _digest_payload(fields)


def verify_event_chain(events: Iterable[Mapping[str, object]]) -> list[str]:
    """Return human-readable integrity problems for an event sequence."""

    problems: list[str] = []
    previous = GENESIS_DIGEST
    seen_seqs: set[int] = set()
    expected_seq = 1
    for event in events:
        seq = event.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            problems.append(f"seq-not-int:{seq!r}")
            continue
        if seq in seen_seqs:
            problems.append(f"duplicate-seq:{seq}")
        seen_seqs.add(seq)
        if seq != expected_seq:
            problems.append(f"seq-gap:{seq}-expected-{expected_seq}")
        expected_seq = seq + 1
        if str(event.get("previous_digest") or "") != previous:
            problems.append(f"broken-chain-at-seq:{seq}")
        recomputed = event_chain_digest(event)
        if str(event.get("event_digest") or "") != recomputed:
            problems.append(f"bad-digest-at-seq:{seq}")
        previous = str(event.get("event_digest") or previous)
    return problems


def read_events_with_tail_recovery(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Read JSONL events, dropping only unusable trailing lines.

    Returns ``(events, dropped_lines)``. This matches durable-append semantics:
    a crash can leave an unparseable partial final line, which is recovered.
    Mid-file corruption stays in the returned list so
    :func:`verify_event_chain` can report it instead of hiding it.
    """

    if not path.is_file():
        return [], 0
    lines = [
        stripped
        for stripped in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if stripped.strip()
    ]
    parsed: list[dict[str, Any] | None] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            value = None
        parsed.append(value if isinstance(value, dict) else None)

    usable_to = len(parsed)
    while usable_to > 0 and parsed[usable_to - 1] is None:
        usable_to -= 1
    dropped = sum(1 for item in parsed if item is None)
    events = [item for item in parsed[:usable_to] if item is not None]
    return events, dropped


class ABJournalWriter:
    """Single-writer durable event journal for one experiment/run/provider."""

    def __init__(
        self,
        *,
        directory: Path,
        experiment_id: str,
        run_id: str,
        provider: str,
        model: str = "",
        transcript_cache: TranscriptReplayCache | None = None,
    ) -> None:
        self.directory = Path(directory)
        self.manifest_path = self.directory / "manifest.json"
        self.events_path = self.directory / "events.jsonl"
        self.experiment_id = str(experiment_id or "").strip()
        self.run_id = str(run_id or "").strip()
        self.provider = str(provider or "").strip()
        self.model = str(model or "").strip()
        self.transcript_cache = transcript_cache or TranscriptReplayCache(self.directory)
        if not self.experiment_id or not self.run_id or not self.provider:
            raise ValueError("experiment_id, run_id, and provider are required")

        events, dropped = read_events_with_tail_recovery(self.events_path)
        problems = verify_event_chain(events)
        if problems:
            raise ValueError(
                f"{self.events_path} failed journal verification "
                f"({problems[:3]}); manual recovery required before writing"
            )
        self._last_seq = int(events[-1].get("seq")) if events else 0
        self._last_digest = (
            str(events[-1].get("event_digest")) if events else GENESIS_DIGEST
        )
        if dropped:
            # Only unparseable trailing lines survive as "dropped" once the
            # chain above verified, so rewriting them away is safe.
            self.rewrite_recovered(events)

        self._load_or_reject_manifest()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._write_manifest()
        self._handle = self.events_path.open("a", encoding="utf-8")

    def rewrite_recovered(self, events: list[dict[str, Any]]) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.events_path.with_name(f".{self.events_path.name}.recover")
        with temporary.open("w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.events_path)

    # -- identity -----------------------------------------------------------

    def _load_or_reject_manifest(self) -> None:
        if not self.manifest_path.is_file():
            return
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict) or payload.get("kind") != MANIFEST_KIND:
            return
        found = (
            str(payload.get("experiment_id") or ""),
            str(payload.get("run_id") or ""),
            str(payload.get("provider") or ""),
        )
        expected = (self.experiment_id, self.run_id, self.provider)
        if found != expected:
            raise ABJournalIdentityMismatch(
                f"{self.directory} belongs to experiment/run/provider {found}; "
                f"refusing to write {expected}"
            )

    # -- low level ----------------------------------------------------------

    def _append_event(
        self,
        *,
        event_type: str,
        case_id: str = "",
        arm: str = "",
        stage: str = "",
        turn: int | None = None,
        prompt: str = "",
        reply: str = "",
        archive_transcript: bool = False,
        failure_kind: str = "",
        facts: Mapping[str, object] | None = None,
    ) -> ABJournalEvent:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown journal event type: {event_type}")
        safe_facts = sanitize_facts(facts)
        if turn is not None:
            safe_facts = {"turn": turn, **safe_facts}

        prompt_text = str(prompt or "")
        reply_text = str(reply or "")
        prompt_digest = digest_text(prompt_text) if prompt_text else ""
        reply_digest = digest_text(reply_text) if reply_text else ""
        ref = self.transcript_cache.ref_for(
            prompt=prompt_text,
            reply=reply_text,
            archive=archive_transcript,
        )
        self._last_seq += 1
        payload: dict[str, object] = {
            "seq": self._last_seq,
            "ts": _timestamp(),
            "run_id": self.run_id,
            "experiment_id": self.experiment_id,
            "case_id": str(case_id or ""),
            "arm": str(arm or ""),
            "provider": self.provider,
            "model": self.model,
            "event_type": event_type,
            "stage": str(stage or ""),
            "prompt_digest": prompt_digest,
            "reply_digest": reply_digest,
            "content_ref": ref.to_payload() if (prompt_text or reply_text) else {},
            "failure_kind": str(failure_kind or ""),
            "facts": safe_facts,
            "previous_digest": self._last_digest,
        }
        payload["event_digest"] = event_chain_digest(payload)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self._handle.write(line + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._last_digest = str(payload["event_digest"])
        return ABJournalEvent(**payload)

    def _read_manifest(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict) or payload.get("kind") != MANIFEST_KIND:
            payload = {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "kind": MANIFEST_KIND,
                "experiment_id": self.experiment_id,
                "run_id": self.run_id,
                "provider": self.provider,
                "model": self.model,
                "transcript_mode": self.transcript_cache.mode,
                "started_at": _timestamp(),
                "status": "running",
            }
        return payload

    def _write_manifest(self) -> None:
        manifest = self._read_manifest()
        manifest["updated_at"] = _timestamp()
        manifest["event_count"] = self._last_seq
        manifest["last_event_digest"] = self._last_digest
        write_json_atomic(self.manifest_path, manifest)

    # -- typed events -------------------------------------------------------

    def append_event(
        self,
        *,
        event_type: str,
        case_id: str = "",
        arm: str = "",
        stage: str = "",
        turn: int | None = None,
        prompt: str = "",
        reply: str = "",
        archive_transcript: bool = False,
        failure_kind: str = "",
        facts: Mapping[str, object] | None = None,
    ) -> ABJournalEvent:
        """Public escape hatch for harness-specific typed events."""

        return self._append_event(
            event_type=event_type,
            case_id=case_id,
            arm=arm,
            stage=stage,
            turn=turn,
            prompt=prompt,
            reply=reply,
            archive_transcript=archive_transcript,
            failure_kind=failure_kind,
            facts=facts,
        )

    def record_run_start(self, *, cases: tuple[str, ...], arms: tuple[str, ...], max_turns: int) -> None:
        self._append_event(
            event_type="run_start",
            stage="run_start",
            facts={"cases": list(cases), "arms": list(arms), "max_turns": max_turns},
        )
        self._write_manifest()

    def record_case_start(self, *, case: str, arm: str, question_chars: int = 0) -> None:
        self._append_event(
            event_type="case_start",
            case_id=case,
            arm=arm,
            stage="case_start",
            facts={"question_chars": question_chars},
        )
        self._write_manifest()

    def record_send_start(
        self,
        *,
        case: str,
        arm: str,
        turn: int,
        prompt: str,
        stage: str = "",
    ) -> None:
        self._append_event(
            event_type="send_start",
            case_id=case,
            arm=arm,
            stage=stage or "send",
            turn=turn,
            prompt=prompt,
        )

    def record_reply(
        self,
        *,
        case: str,
        arm: str,
        turn: int,
        prompt: str,
        reply: str,
        stage: str = "",
    ) -> None:
        self._append_event(
            event_type="reply",
            case_id=case,
            arm=arm,
            stage=stage or "reply",
            turn=turn,
            prompt=prompt,
            reply=reply,
            archive_transcript=True,
        )

    def record_send_error(
        self,
        *,
        case: str,
        arm: str,
        turn: int,
        error: str,
        failure_kind: str = "",
        stage: str = "",
        provider_failure: Mapping[str, object] | None = None,
    ) -> None:
        facts: dict[str, object] = {"error_class": str(error).split(":", 1)[0][:80]}
        if provider_failure:
            facts["provider_failure"] = dict(provider_failure)
        self._append_event(
            event_type="send_error",
            case_id=case,
            arm=arm,
            stage=stage or "send",
            turn=turn,
            failure_kind=failure_kind or "send_error",
            facts=facts,
        )

    def record_observation(
        self,
        *,
        case: str,
        arm: str,
        observation: str,
        facts: Mapping[str, object] | None = None,
    ) -> None:
        """Record a typed provider observation (timeout / adapter_failure)."""

        if observation not in ("timeout", "adapter_failure"):
            raise ValueError(f"unsupported observation: {observation}")
        self._append_event(
            event_type=observation,
            case_id=case,
            arm=arm,
            stage=observation,
            failure_kind=facts.get("failure_kind") if facts else "",
            facts=facts,
        )

    def record_case_complete(
        self,
        *,
        case: str,
        arm: str,
        row: Mapping[str, object],
    ) -> None:
        turns_raw = row.get("turns")
        try:
            turns = max(0, int(turns_raw))
        except (TypeError, ValueError):
            turns = 0
        score = row.get("score")
        facts: dict[str, object] = {
            "ok": bool(row.get("ok")),
            "stop_reason": str(row.get("stop_reason") or row.get("error") or "")[:120],
            "turns": turns,
        }
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            facts["score"] = round(float(score), 4)
        self._append_event(
            event_type="case_complete",
            case_id=case,
            arm=arm,
            stage="case_complete",
            facts=facts,
        )
        self._write_manifest()

    def record_run_complete(self, *, rows: int, status: str = "done") -> None:
        self._append_event(
            event_type="run_complete",
            stage="run_complete",
            facts={"rows": rows},
        )
        manifest = self._read_manifest()
        manifest["status"] = status
        self._write_manifest_with(manifest)

    def _write_manifest_with(self, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = _timestamp()
        manifest["event_count"] = self._last_seq
        manifest["last_event_digest"] = self._last_digest
        write_json_atomic(self.manifest_path, manifest)

    def close(self) -> None:
        handle = getattr(self, "_handle", None)
        if handle is not None:
            handle.close()
            self._handle = None


class ABJournalReader:
    """Read-side helpers: replay, verification, resume, and repair."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.manifest_path = self.directory / "manifest.json"
        self.events_path = self.directory / "events.jsonl"

    def manifest(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def events(self) -> list[dict[str, Any]]:
        events, _dropped = read_events_with_tail_recovery(self.events_path)
        return events

    def verify_hash_chain(self) -> list[str]:
        return verify_event_chain(self.events())

    def recover_tail(self) -> int:
        """Drop corrupt/unusable trailing lines; return the dropped count."""

        events, dropped = read_events_with_tail_recovery(self.events_path)
        if dropped and self.events_path.is_file():
            temporary = self.events_path.with_name(f".{self.events_path.name}.recover")
            with temporary.open("w", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.events_path)
        return dropped

    def completed_case_keys(self) -> list[tuple[str, str]]:
        keys: list[tuple[str, str]] = []
        for event in self.events():
            if event.get("event_type") != "case_complete":
                continue
            key = (str(event.get("case_id") or ""), str(event.get("arm") or ""))
            if key not in keys:
                keys.append(key)
        return keys


__all__ = [
    "ABJournalEvent",
    "ABJournalIdentityMismatch",
    "ABJournalReader",
    "ABJournalWriter",
    "EVENT_TYPES",
    "GENESIS_DIGEST",
    "MANIFEST_KIND",
    "TRANSCRIPT_MODE_ARCHIVE",
    "TRANSCRIPT_MODE_DIGEST_ONLY",
    "TranscriptReplayCache",
    "TranscriptRef",
    "digest_text",
    "journal_directory_for",
    "read_events_with_tail_recovery",
    "sanitize_facts",
    "verify_event_chain",
]
