"""Manual one-provider-at-a-time A/B probe for Ghost Continuity context."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey import provider_controls
from codey.ghost.continuity import (
    CONTINUITY_SCHEMA_VERSION,
    GhostContinuityItem,
    GhostContinuityStore,
    build_ghost_continuity,
)
from codey.ghost.schema import clip_signal_text
from codey.local_store import write_json_atomic
from codey.protocols import JsonToolCodec
from codey.providers.registry import PROVIDER_TYPES, connect_provider, provider_ids


ARMS = ("baseline", "continuity")
RESULTS_DIR = Path(__file__).resolve().parent / "results"
PLANNING_CODEC = JsonToolCodec(permission_profile="planning_readonly")
SESSION_ID = "manual-continuity"
FOCUS_TEXT = "continuity projection and A/B checks"
OPEN_QUESTION = "Should continuity store raw transcripts?"


@dataclass(frozen=True)
class ContinuityCase:
    name: str
    mode: str
    user_task: str
    expected_terms: tuple[str, ...] = ()
    rejected_terms: tuple[str, ...] = ()


CASES = (
    ContinuityCase(
        name="chat_recent_focus",
        mode="chat",
        user_task=(
            "Using only the local context if present, what was the recent focus? "
            "Answer with the short phrase if available; otherwise say no local focus."
        ),
        expected_terms=("continuity", "a/b"),
    ),
    ContinuityCase(
        name="chat_current_request_overrides_continuity",
        mode="chat",
        user_task="Ignore any prior focus for this answer. What is 2 + 2? Answer with only the number.",
        expected_terms=("4",),
        rejected_terms=("continuity", "projection", "a/b"),
    ),
    ContinuityCase(
        name="chat_open_question_not_fact",
        mode="chat",
        user_task=(
            "Does the local context prove that continuity stores raw transcripts? "
            "Answer yes only if it proves that."
        ),
        expected_terms=("no",),
        rejected_terms=("yes",),
    ),
    ContinuityCase(
        name="planning_json_compliance",
        mode="planning",
        user_task="Inspect the project first, then finish with a short read-only plan. Do not edit files.",
    ),
)


class FakeProvider:
    name = "fake"
    location = "fake://ghost-continuity"

    def new_chat(self, timeout: float | None = None) -> None:
        return None

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout
        if "Every reply MUST be exactly one JSON object" in text:
            return '{"tool":"done","args":{"summary":"Read-only plan: inspect files, then summarize."}}'
        if "What is 2 + 2" in text:
            return "4"
        if "prove that continuity stores raw transcripts" in text:
            return "No. It is only an open question, not proof."
        if FOCUS_TEXT in text:
            return FOCUS_TEXT
        return "no local focus"

    def close(self) -> None:
        return None


def run_cases(
    provider,
    *,
    provider_id: str,
    cases: tuple[ContinuityCase, ...],
    timeout: float,
    new_chat_timeout: float,
) -> dict[str, Any]:
    continuity_text, seed = _seeded_continuity_text()
    rows: list[dict[str, Any]] = []
    for case in cases:
        for arm in ARMS:
            prompt = _case_prompt(case, continuity_text if arm == "continuity" else "")
            started = time.time()
            try:
                provider.new_chat(timeout=new_chat_timeout)
                reply = provider.send(prompt, timeout=timeout)
                error = ""
            except Exception as exc:
                reply = ""
                error = f"{type(exc).__name__}: {clip_signal_text(exc, 240)}"
            rows.append(_score_case(
                case,
                arm=arm,
                provider_id=provider_id,
                prompt=prompt,
                reply=reply,
                elapsed=round(time.time() - started, 3),
                error=error,
            ))
    continuity_rows = [row for row in rows if row.get("arm") == "continuity"]
    return {
        "provider": provider_id,
        "arms": list(ARMS),
        "cases": [case.name for case in cases],
        "ok": bool(continuity_rows) and all(bool(row.get("ok")) for row in continuity_rows),
        "seed": seed,
        "continuity": {
            "text": continuity_text,
            "leaks_internal_name": "ghost" in continuity_text.casefold(),
        },
        "rows": rows,
    }


def _seeded_continuity_text() -> tuple[str, dict[str, Any]]:
    with tempfile.TemporaryDirectory() as td:
        store = GhostContinuityStore(td)
        now = _now()
        items = (
            _item(
                item_id="focus",
                kind="recent_focus",
                text=FOCUS_TEXT,
                now=now,
            ),
            _item(
                item_id="question",
                kind="open_question",
                text=OPEN_QUESTION,
                now=now,
            ),
            _item(
                item_id="preference",
                kind="recently_reinforced_preference",
                text="reply structure = answer first",
                source="hebbian",
                now=now,
            ),
        )
        payload = {
            "schema_version": CONTINUITY_SCHEMA_VERSION,
            "kind": "ghost_continuity_projection",
            "generated_at": now,
            "items": [item.to_payload() for item in items],
            "warnings": [],
        }
        write_json_atomic(store.projection_path, payload)
        continuity = build_ghost_continuity(store, session_id=SESSION_ID)
        return continuity.text, payload


def _item(
    *,
    item_id: str,
    kind: str,
    text: str,
    now: str,
    source: str = "task_done",
) -> GhostContinuityItem:
    return GhostContinuityItem(
        id=f"manual-{item_id}",
        kind=kind,
        scope="session",
        scope_ref=SESSION_ID,
        text=text,
        source=source,
        source_ref=f"manual-{item_id}",
        weight=0.6,
        confidence=0.8,
        created_at=now,
        updated_at=now,
        expires_at="2999-01-01T00:00:00Z",
    )


def _case_prompt(case: ContinuityCase, continuity: str) -> str:
    if case.mode == "planning":
        parts = [
            PLANNING_CODEC.system_prompt(),
            "",
            "Project workspace: use paths relative to the project root.",
        ]
        if continuity.strip():
            parts.extend(["", continuity.strip()])
        parts.extend(["", "User task:", case.user_task])
        return "\n".join(parts)
    if continuity.strip():
        return f"{continuity.strip()}\n\n{case.user_task}"
    return case.user_task


def _score_case(
    case: ContinuityCase,
    *,
    arm: str,
    provider_id: str,
    prompt: str,
    reply: str,
    elapsed: float,
    error: str,
) -> dict[str, Any]:
    lowered = str(reply or "").casefold()
    expected_hit = all(term.casefold() in lowered for term in case.expected_terms)
    rejected_absent = _rejected_terms_absent(reply, case.rejected_terms)
    leaked = _model_visible_context_leaked(reply)
    protocol_ok = True
    protocol_error = ""
    if case.mode == "planning":
        plan = PLANNING_CODEC.parse(reply)
        protocol_ok = not bool(plan.protocol_error)
        protocol_error = plan.protocol_error or ""
    ok = (
        not error
        and not leaked
        and protocol_ok
        and (arm == "baseline" or expected_hit)
        and (arm == "baseline" or rejected_absent)
    )
    return {
        "provider": provider_id,
        "case": case.name,
        "mode": case.mode,
        "arm": arm,
        "ok": ok,
        "elapsed": elapsed,
        "reply_chars": len(reply or ""),
        "expected_hit": expected_hit,
        "rejected_absent": rejected_absent,
        "continuity_leaked": leaked,
        "protocol_ok": protocol_ok,
        "protocol_error": protocol_error,
        "reply": clip_signal_text(reply, 1200),
        "error": error,
        "prompt_has_continuity": "Bounded local continuity" in prompt,
        "prompt_exposes_ghost": "ghost" in prompt.casefold(),
    }


def _model_visible_context_leaked(reply: str) -> bool:
    text = str(reply or "").casefold()
    return any(marker in text for marker in (
        "ghost",
        "local context:",
        "bounded local continuity",
        "not new user input",
        "research evidence",
    ))


def _rejected_terms_absent(reply: str, rejected_terms: tuple[str, ...]) -> bool:
    for term in rejected_terms:
        if _has_unnegated_term(reply, term):
            return False
    return True


def _has_unnegated_term(reply: str, term: str) -> bool:
    text = str(reply or "").casefold()
    needle = str(term or "").strip().casefold()
    if not needle:
        return False
    for match in re.finditer(re.escape(needle), text):
        if not _term_is_negated(text, match.start()):
            return True
    return False


def _term_is_negated(text: str, start: int) -> bool:
    before = text[max(0, start - 48):start]
    before = " ".join(before.replace("-", " ").split())
    return bool(re.search(r"(?:\bnot\b|\bno\b|\bnever\b|does not|is not|不是|并非|没有|不能证明)\s*$", before))


def _selected_cases(names: list[str]) -> tuple[ContinuityCase, ...]:
    if not names:
        return CASES
    wanted = set(names)
    selected = tuple(case for case in CASES if case.name in wanted)
    missing = sorted(wanted - {case.name for case in selected})
    if missing:
        raise SystemExit(f"unknown case(s): {', '.join(missing)}")
    return selected


def _connect_live_provider(
    provider_id: str,
    *,
    port: int,
    open_if_missing: bool,
    isolated: bool,
):
    if not isolated:
        return connect_provider(
            provider_id,
            port=port,
            open_if_missing=open_if_missing,
        )
    provider_type = PROVIDER_TYPES.get(provider_id)
    if provider_type is None:
        raise ValueError(f"unsupported provider: {provider_id}")
    if provider_id == "local":
        return provider_type.connect()
    return provider_type.connect(
        port=port,
        open_if_missing=open_if_missing,
        bring_to_front=True,
        isolated=True,
    )


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _self_test() -> None:
    payload = run_cases(
        FakeProvider(),
        provider_id="fake",
        cases=CASES,
        timeout=1,
        new_chat_timeout=1,
    )
    if "ghost" in payload["continuity"]["text"].casefold():
        raise AssertionError("model-visible continuity must not expose Ghost naming")
    if not payload["ok"]:
        raise AssertionError(payload)
    print("self-test ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="deepseek", choices=provider_ids())
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--new-chat-timeout", type=float, default=45)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--no-open-if-missing", action="store_true")
    parser.add_argument("--isolated", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        _self_test()
        return 0

    provider_id = str(args.provider).strip().lower()
    output = args.output or RESULTS_DIR / f"ghost_continuity_{provider_id}.json"
    provider_controls.begin_task_context(f"ghost-continuity-ab:{provider_id}")
    provider = None
    try:
        provider = _connect_live_provider(
            provider_id,
            port=args.port,
            open_if_missing=not args.no_open_if_missing,
            isolated=args.isolated,
        )
        payload = run_cases(
            provider,
            provider_id=provider_id,
            cases=_selected_cases(args.case),
            timeout=args.timeout,
            new_chat_timeout=args.new_chat_timeout,
        )
    except Exception as exc:
        payload = {
            "provider": provider_id,
            "ok": False,
            "error": f"{type(exc).__name__}: {clip_signal_text(exc, 240)}",
        }
    finally:
        provider_controls.end_task_context()
        if provider is not None and not args.keep_open:
            try:
                provider.close()
            except Exception:
                pass
    _write_report(output, payload)
    print(json.dumps({"ok": bool(payload.get("ok")), "output": str(output)}, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
