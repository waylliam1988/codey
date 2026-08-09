"""Manual one-provider-at-a-time A/B probe for local memory prompt context."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey import provider_controls
from codey.ghost.directive import render_ghost_directive
from codey.ghost.hebbian import GhostNode
from codey.ghost.schema import clip_signal_text
from codey.protocols import JsonToolCodec
from codey.providers.registry import PROVIDER_TYPES, connect_provider, provider_ids


ARMS = ("baseline", "directive")
RESULTS_DIR = Path(__file__).resolve().parent / "results"
PLANNING_CODEC = JsonToolCodec(permission_profile="planning_readonly")


@dataclass(frozen=True)
class DirectiveCase:
    name: str
    mode: str
    user_task: str
    expected_terms: tuple[str, ...] = ()
    rejected_terms: tuple[str, ...] = ()


CASES = (
    DirectiveCase(
        name="chat_correction_hit",
        mode="chat",
        user_task="For this project, what is the local memory state backend? Answer briefly.",
        expected_terms=("json",),
        rejected_terms=("sqlite",),
    ),
    DirectiveCase(
        name="chat_no_directive_leak",
        mode="chat",
        user_task="Explain why local memory should stay auditable. Use a direct answer.",
    ),
    DirectiveCase(
        name="planning_json_compliance",
        mode="planning",
        user_task="Inspect the project first, then finish with a short plan. Do not edit files.",
    ),
)


class FakeProvider:
    name = "fake"
    location = "fake://ghost-directive"

    def new_chat(self, timeout: float | None = None) -> None:
        return None

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout
        if "Every reply MUST be exactly one JSON object" in text:
            return '{"tool":"done","args":{"summary":"Read-only plan: inspect files, then summarize."}}'
        if "Local Context:" in text and "local memory state backend" in text:
            return "JSON projection, backed by a local Hebbian event log."
        if "local memory state backend" in text:
            return "It might be SQLite."
        return "Auditable memory means users can inspect, export, and delete what was remembered."

    def close(self) -> None:
        return None


def run_cases(
    provider,
    *,
    provider_id: str,
    cases: tuple[DirectiveCase, ...],
    timeout: float,
    new_chat_timeout: float,
) -> list[dict[str, Any]]:
    directive_text = _directive_text()
    rows: list[dict[str, Any]] = []
    for case in cases:
        for arm in ARMS:
            prompt = _case_prompt(case, directive_text if arm == "directive" else "")
            started = time.time()
            try:
                provider.new_chat(timeout=new_chat_timeout)
                reply = provider.send(prompt, timeout=timeout)
                error = ""
            except Exception as exc:
                reply = ""
                error = f"{type(exc).__name__}: {clip_signal_text(exc, 240)}"
            elapsed = round(time.time() - started, 3)
            rows.append(_score_case(
                case,
                arm=arm,
                provider_id=provider_id,
                prompt=prompt,
                reply=reply,
                elapsed=elapsed,
                error=error,
            ))
    return rows


def _directive_text() -> str:
    directive = render_ghost_directive((
        _node(
            node_id="correction-json-state",
            kind="correction",
            label="Local memory state backend is bounded local JSON projection plus JSONL audit, not SQLite.",
            conflict_key="correction:ghost_state_backend",
            value_key="json_projection_jsonl",
            weight=0.55,
        ),
        _node(
            node_id="style-answer-first",
            kind="style_preference",
            label="Prefer concise, answer-first replies.",
            conflict_key="style_preference:reply_structure",
            value_key="answer_first_concise",
            weight=0.45,
        ),
    ))
    return directive.text


def _node(
    *,
    node_id: str,
    kind: str,
    label: str,
    conflict_key: str,
    value_key: str,
    weight: float,
) -> GhostNode:
    now = _now()
    return GhostNode(
        id=node_id,
        kind=kind,
        label=label,
        conflict_key=conflict_key,
        value_key=value_key,
        status="active",
        scope="user",
        scope_ref="",
        weight=weight,
        confidence=0.9,
        candidate_ids=(f"{node_id}-candidate",),
        evidence_refs=(f"{node_id}:1",),
        created_at=now,
        updated_at=now,
        last_reinforced_at=now,
    )


def _case_prompt(case: DirectiveCase, directive: str) -> str:
    if case.mode == "planning":
        parts = [
            PLANNING_CODEC.system_prompt(),
            "",
            "Project workspace: use paths relative to the project root.",
        ]
        if directive.strip():
            parts.extend(["", directive.strip()])
        parts.extend(["", "User task:", case.user_task])
        return "\n".join(parts)
    if directive.strip():
        return f"{directive.strip()}\n\n{case.user_task}"
    return case.user_task


def _score_case(
    case: DirectiveCase,
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
        "directive_leaked": leaked,
        "protocol_ok": protocol_ok,
        "protocol_error": protocol_error,
        "reply": clip_signal_text(reply, 1200),
        "error": error,
        "prompt_has_directive": "Local Context:" in prompt,
    }


def _model_visible_context_leaked(reply: str) -> bool:
    text = str(reply or "").casefold()
    return any(marker in text for marker in (
        "ghost",
        "ghost directive",
        "local context:",
        "confirmed local memory",
        "not new user input",
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
    before = text[max(0, start - 36):start]
    before = " ".join(before.replace("—", " ").replace("-", " ").split())
    return bool(re.search(r"(?:\bnot\b|\bno\b|\bnever\b|不是|并非|非|不使用|不是用)\s*$", before))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _selected_cases(names: list[str]) -> tuple[DirectiveCase, ...]:
    if not names:
        return CASES
    wanted = set(names)
    selected = tuple(case for case in CASES if case.name in wanted)
    missing = sorted(wanted - {case.name for case in selected})
    if missing:
        raise SystemExit(f"unknown case(s): {', '.join(missing)}")
    return selected


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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


def _should_close_provider(*, keep_open: bool, isolated: bool) -> bool:
    del isolated
    return not keep_open


def _self_test() -> None:
    if "ghost" in _directive_text().casefold():
        raise AssertionError("model-visible context must not expose Ghost naming")
    rows = run_cases(
        FakeProvider(),
        provider_id="fake",
        cases=CASES,
        timeout=1,
        new_chat_timeout=1,
    )
    failures = [row for row in rows if row["arm"] == "directive" and not row["ok"]]
    if failures:
        raise AssertionError(f"self-test failures: {failures}")
    if _should_close_provider(keep_open=True, isolated=False):
        raise AssertionError("--keep-open should keep non-isolated providers open")
    if _should_close_provider(keep_open=True, isolated=True):
        raise AssertionError("--keep-open should keep isolated providers open")
    if not _should_close_provider(keep_open=False, isolated=False):
        raise AssertionError("providers should close by default")
    if not _rejected_terms_absent("bounded JSON projection, not SQLite", ("sqlite",)):
        raise AssertionError("negated rejected term should not fail")
    if _rejected_terms_absent("It uses SQLite", ("sqlite",)):
        raise AssertionError("unnegated rejected term should fail")
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
    parser.add_argument(
        "--isolated",
        action="store_true",
        help="launch an isolated CDP browser port instead of reusing remembered ports",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        _self_test()
        return 0

    provider_id = str(args.provider).strip().lower()
    output = args.output or RESULTS_DIR / f"ghost_directive_{provider_id}.json"
    rows: list[dict[str, Any]]
    run_error = ""
    provider_controls.begin_task_context(f"ghost-directive-ab:{provider_id}")
    provider = None
    try:
        provider = _connect_live_provider(
            provider_id,
            port=args.port,
            open_if_missing=not args.no_open_if_missing,
            isolated=args.isolated,
        )
        rows = run_cases(
            provider,
            provider_id=provider_id,
            cases=_selected_cases(args.case),
            timeout=args.timeout,
            new_chat_timeout=args.new_chat_timeout,
        )
    except Exception as exc:
        run_error = f"{type(exc).__name__}: {clip_signal_text(exc, 240)}"
        rows = [{
            "provider": provider_id,
            "case": "connect_or_run",
            "mode": "connect",
            "arm": "directive",
            "ok": False,
            "error": run_error,
        }]
    finally:
        provider_controls.end_task_context()
        if provider is not None and _should_close_provider(
            keep_open=args.keep_open,
            isolated=args.isolated,
        ):
            try:
                provider.close()
            except Exception:
                pass

    directive_rows = [row for row in rows if row.get("arm") == "directive"]
    payload = {
        "provider": provider_id,
        "arms": list(ARMS),
        "cases": [case.name for case in _selected_cases(args.case)],
        "ok": bool(directive_rows) and all(bool(row.get("ok")) for row in directive_rows),
        "error": run_error,
        "rows": rows,
    }
    _write_report(output, payload)
    print(json.dumps({"ok": payload["ok"], "output": str(output)}, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
