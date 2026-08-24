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
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from tests.manual.ab_journal import ABJournalWriter, journal_directory_for

MAX_RESULT_BYTES = 1024 * 1024


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
    provider, which is what scripted self-tests use. Timeouts are pass-through
    when not configured, so wrappers that manage their own timeouts keep
    working unchanged.
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
        effective = timeout if timeout is not None else self.new_chat_timeout
        return self.provider.new_chat(timeout=effective)

    def send(self, text: str, timeout: float | None = None) -> str:
        prompt = str(text or "")
        effective = timeout if timeout is not None else self.timeout
        self.send_index += 1
        turn = self.send_index
        self.prompt_chars += len(prompt)
        if self.journal is not None:
            self.journal.record_send_start(case=self.case, arm=self.arm, turn=turn, prompt=prompt)
        try:
            reply_text = str(self.provider.send(prompt, timeout=effective))
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

    def close(self) -> None:
        return self.provider.close()


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
    "FixtureDocument",
    "FixtureSearchProvider",
    "OutputProviderMismatch",
    "TracingProvider",
    "bounded_error_row",
    "case_names",
    "expected_matrix_keys",
    "fixture_material_phase",
    "fixture_url_policy_bypass",
    "interleaved_arm_schedule",
    "journal_directory_for_output",
    "load_or_new_payload",
    "matrix_complete",
    "merge_unique_names",
    "new_payload",
    "normalize_payload_metadata",
    "timestamp",
    "write_json_atomic",
    "write_payload_bounded",
]
