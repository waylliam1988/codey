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
from typing import Any, Iterable, Iterator, Mapping, Sequence

from tests.manual.ab_journal import (
    TRANSCRIPT_MODE_ARCHIVE,
    TRANSCRIPT_MODE_DIGEST_ONLY,
    ABJournalWriter,
    TranscriptReplayCache,
    journal_directory_for,
)

MAX_RESULT_BYTES = 1024 * 1024

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
        super().__init__(
            f"{path} was created for provider {found!r}; refusing to reuse it for {expected!r}"
        )


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
        self.new_chat_timeout = (
            None if new_chat_timeout is None else max(1.0, float(new_chat_timeout))
        )
        self.send_index = 0
        self.reply_count = 0
        self.prompt_chars = 0
        self.reply_chars = 0
        self.prompts: list[str] = []
        self.replies: list[str] = []
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
            self.journal.record_reply(
                case=self.case, arm=self.arm, turn=turn, prompt=prompt, reply=reply_text
            )
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
    return {
        (case, index + 1) for case in tuple(cases) for index in range(repeat_count)
    }


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


def journal_directory_for_output(output: Path) -> Path:
    return journal_directory_for(output)


def git_state(repo: Path | None = None) -> dict[str, Any]:
    """Commit + dirty flag for A/B evidence, failing soft to unknowns."""

    state: dict[str, Any] = {"git_commit": "", "git_dirty": None}
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo) if repo else None,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo) if repo else None,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return state
    state["git_commit"] = commit
    state["git_dirty"] = bool(status.strip())
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
    provider_error_class: str = AB_FAILURE_NONE,
    codey_failure_class: str = AB_FAILURE_NONE,
    repo: Path | None = None,
) -> dict[str, Any]:
    """The fixed per-arm evidence schema every manual A/B must persist."""

    if provider_error_class not in AB_FAILURE_CLASSES:
        raise ValueError(f"unknown provider_error_class: {provider_error_class}")
    if codey_failure_class not in AB_FAILURE_CLASSES:
        raise ValueError(f"unknown codey_failure_class: {codey_failure_class}")
    manifest: dict[str, Any] = {
        "suite": str(suite or "").strip(),
        "provider": str(provider or "").strip(),
        "arms": [str(arm) for arm in arms],
        "cases": [str(case) for case in cases],
        "max_turns": max(1, int(max_turns)),
        "journal_dir": str(journal_dir) if journal_dir is not None else "",
        "transcript_mode": str(transcript_mode or "off"),
        "started_at": started_at,
        "finished_at": finished_at,
        "stop_reason": stop_reason,
        "provider_error_class": provider_error_class,
        "codey_failure_class": codey_failure_class,
    }
    manifest.update(git_state(repo))
    return manifest


def write_arm_manifest(output: Path, manifest: Mapping[str, Any]) -> Path:
    """Persist the arm manifest next to its result JSON, atomically."""

    path = output.with_name(f"{output.stem}-manifest.json")
    write_json_atomic(path, dict(manifest))
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
    resolved_dir = journal_dir or output.parent / f"{output.stem}-journal"
    writer = ABJournalWriter(
        directory=resolved_dir,
        experiment_id=experiment_id,
        run_id=run_id or f"{provider_id}-{output.stem}",
        provider=provider_id,
        transcript_cache=TranscriptReplayCache(resolved_dir, mode=mode),
    )
    if writer.event_count == 0:
        writer.record_run_start(
            cases=tuple(case_names),
            arms=tuple(arms),
            max_turns=max(4, int(max_turns)),
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
        key = (str(row.get("case") or ""), str(row.get("arm") or ""))
        rows[:] = [
            existing
            for existing in rows
            if (
                (str(existing.get("case") or ""), str(existing.get("arm") or "")) != key
                or not existing.get("error")
            )
        ]
    rows.append(row)


def classify_ab_failure(exc: BaseException) -> str:
    """Coarse first-pass classification for crashed (case, arm) cells."""

    text = f"{type(exc).__name__}: {exc}".lower()
    if any(token in text for token in ("timeout", "connection", "http", "socket")):
        return AB_FAILURE_PROVIDER
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
def fixture_url_policy_bypass(
    allowed_prefixes: Sequence[str] = ("https://source-a.test/", "https://source-b.test/"),
) -> Iterator[None]:
    """Allow *.test fixture hosts through the research URL policy, scoped."""

    from codey.research import plan_executor as research_plan_executor_module
    from codey.research import tools as research_tools_module
    from codey.research import url_policy as research_url_policy_module

    original = (
        research_url_policy_module.check_fetch_url,
        research_tools_module.check_fetch_url,
        research_plan_executor_module.check_fetch_url,
    )

    def _allow_fixture_urls(url: str, *, resolve: bool = True) -> str | None:
        text = str(url or "").strip().lower()
        if text.startswith(tuple(allowed_prefixes)):
            return None
        return original[0](url, resolve=resolve)

    research_url_policy_module.check_fetch_url = _allow_fixture_urls
    research_tools_module.check_fetch_url = _allow_fixture_urls
    research_plan_executor_module.check_fetch_url = _allow_fixture_urls
    try:
        yield
    finally:
        (
            research_url_policy_module.check_fetch_url,
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
    "FixtureDocument",
    "FixtureSearchProvider",
    "OutputProviderMismatch",
    "TRANSCRIPT_MODE_FLAGS",
    "TracingProvider",
    "append_or_replace_failed_row",
    "bounded_error_row",
    "build_arm_manifest",
    "case_names",
    "classify_ab_failure",
    "expected_matrix_keys",
    "fixture_material_phase",
    "fixture_url_policy_bypass",
    "git_state",
    "interleaved_arm_schedule",
    "journal_directory_for_output",
    "load_or_new_payload",
    "matrix_complete",
    "merge_unique_names",
    "new_payload",
    "normalize_payload_metadata",
    "open_journal_for_output",
    "timestamp",
    "write_arm_manifest",
    "write_json_atomic",
    "write_payload_bounded",
]
