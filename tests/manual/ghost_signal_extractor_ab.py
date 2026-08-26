"""Manual live probe for Ghost Signal Extractor.

This probe asks one live provider at a time to classify explicit Ghost learning
signals. It does not edit files, execute local tools, write accepted memory, or
change Codey production behavior.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.providers import controls as provider_controls
from codey.ghost.extractor import GhostSignalExtractor
from codey.ghost.schema import clip_signal_text, quote_is_grounded
from codey.providers.registry import PROVIDER_TYPES, connect_provider, provider_ids


RESULTS_DIR = Path(tempfile.gettempdir())
ARMS = ("baseline", "extractor")


@dataclass(frozen=True)
class GhostCase:
    name: str
    user_text: str
    expected_kinds: tuple[str, ...]


CASES = (
    GhostCase(
        name="style_conclusion_first",
        user_text="以后请先给结论，再解释原因。",
        expected_kinds=("style_preference",),
    ),
    GhostCase(
        name="avoid_marketing_tone",
        user_text="不要写营销味，我更喜欢朴素一点。",
        expected_kinds=("style_preference",),
    ),
    GhostCase(
        name="correction",
        user_text="你刚才说错了，正确是这个项目不应该直接搬某个 torch 模块。",
        expected_kinds=("correction",),
    ),
    GhostCase(
        name="research_interest",
        user_text="这个研究方向很重要，之后要继续关注战争、铜和氦气之间的关系。",
        expected_kinds=("research_interest",),
    ),
    GhostCase(
        name="action_tendency",
        user_text="这种问题以后先查证据再回答，不要直接凭印象说。",
        expected_kinds=("action_tendency",),
    ),
    GhostCase(
        name="no_signal_continue",
        user_text="继续。",
        expected_kinds=(),
    ),
    GhostCase(
        name="no_signal_thanks",
        user_text="好的，谢谢。",
        expected_kinds=(),
    ),
)


class FakeProvider:
    name = "fake"
    location = "fake://ghost"

    def new_chat(self, timeout: float | None = None) -> None:
        return None

    def send(self, text: str, timeout: float | None = None) -> str:
        user_text = _prompt_user_text(text)
        if "以后请先给结论" in user_text:
            return _json_signal("style_preference", "user", "Prefer conclusion-first answers.", "以后请先给结论")
        if "不要写营销味" in user_text:
            return _json_signal("style_preference", "user", "Avoid marketing tone.", "不要写营销味")
        if "你刚才说错了" in user_text:
            return _json_signal("correction", "session", "Do not directly port the torch module.", "正确是这个项目不应该直接搬某个 torch 模块")
        if "这个研究方向很重要" in user_text:
            return _json_signal("research_interest", "user", "Track war/copper/helium links.", "这个研究方向很重要")
        if "先查证据再回答" in user_text:
            return _json_signal("action_tendency", "user", "Check evidence before answering.", "先查证据再回答")
        return '{"signals":[]}'

    def close(self) -> None:
        return None


def _json_signal(kind: str, scope: str, summary: str, quote: str) -> str:
    return json.dumps(
        {
            "signals": [{
                "kind": kind,
                "scope": scope,
                "summary": summary,
                "evidence_quote": quote,
                "confidence": 0.9,
            }],
        },
        ensure_ascii=False,
    )


def _prompt_user_text(prompt: str) -> str:
    marker = "\nUser message:\n"
    if marker not in prompt:
        return prompt
    tail = prompt.split(marker, 1)[1]
    return tail.split("\nAssistant reply context", 1)[0].split("\nReturn exactly one JSON object", 1)[0]


def run_cases(
    provider,
    *,
    provider_id: str,
    cases: tuple[GhostCase, ...],
    timeout: float,
    new_chat_timeout: float,
) -> list[dict[str, Any]]:
    extractor = GhostSignalExtractor()
    rows: list[dict[str, Any]] = []
    for case in cases:
        rows.append(_score_case(
            case,
            GhostSignalExtractor().extract_from_reply('{"signals":[]}', user_text=case.user_text),
            provider_id=provider_id,
            arm="baseline",
            elapsed=0.0,
        ))
        started = time.time()
        try:
            provider.new_chat(timeout=new_chat_timeout)
            result = extractor.extract(
                provider=provider,
                user_text=case.user_text,
                provider_id=provider_id,
                timeout=timeout,
            )
            error = ""
        except Exception as exc:
            result = None
            error = f"{type(exc).__name__}: {exc}"
        elapsed = round(time.time() - started, 3)
        rows.append(_score_case(
            case,
            result,
            provider_id=provider_id,
            arm="extractor",
            elapsed=elapsed,
            error=error,
        ))
    return rows


def _score_case(
    case: GhostCase,
    result,
    *,
    provider_id: str,
    arm: str,
    elapsed: float,
    error: str = "",
) -> dict[str, Any]:
    if result is None:
        return {
            "provider": provider_id,
            "arm": arm,
            "case": case.name,
            "ok": False,
            "error": error,
            "elapsed": elapsed,
        }
    kinds = tuple(signal.kind for signal in result.signals)
    expected = set(case.expected_kinds)
    observed = set(kinds)
    grounded = all(quote_is_grounded(signal.evidence_quote, case.user_text) for signal in result.signals)
    no_false_positive = bool(expected) or not observed
    kind_hit = expected.issubset(observed)
    ok = bool(result.ok and grounded and no_false_positive and kind_hit)
    return {
        "provider": provider_id,
        "arm": arm,
        "case": case.name,
        "ok": ok,
        "expected_kinds": list(case.expected_kinds),
        "observed_kinds": list(kinds),
        "parse_ok": result.ok,
        "grounded": grounded,
        "false_positive": not no_false_positive,
        "diagnostics": list(result.diagnostics),
        "signal_count": len(result.signals),
        "elapsed": elapsed,
        "error": error,
    }


def _selected_cases(names: list[str]) -> tuple[GhostCase, ...]:
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


def _should_close_provider(*, keep_open: bool, isolated: bool) -> bool:
    # Non-isolated Session.close() only releases Playwright/CDP automation; it
    # does not close the durable provider tab. Skipping it can leave the next
    # manual probe with a half-stale CDP connection.
    return (not keep_open) or (not isolated)


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


def _self_test() -> None:
    rows = run_cases(
        FakeProvider(),
        provider_id="fake",
        cases=CASES,
        timeout=1,
        new_chat_timeout=1,
    )
    failures = [
        row
        for row in rows
        if row.get("arm") == "extractor" and not row.get("ok")
    ]
    if failures:
        raise AssertionError(f"self-test failures: {failures}")
    baseline_signal_hits = [
        row
        for row in rows
        if row.get("arm") == "baseline" and row.get("observed_kinds")
    ]
    if baseline_signal_hits:
        raise AssertionError(f"baseline should never emit signals: {baseline_signal_hits}")
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
    output = args.output or RESULTS_DIR / f"ghost_signal_extractor_{provider_id}.json"
    cases = _selected_cases(args.case)
    rows: list[dict[str, Any]] = []
    run_error = ""
    provider_controls.begin_task_context(f"ghost-signal-extractor-ab:{provider_id}")
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
            cases=cases,
            timeout=args.timeout,
            new_chat_timeout=args.new_chat_timeout,
        )
    except Exception as exc:
        run_error = f"{type(exc).__name__}: {clip_signal_text(exc, 240)}"
        rows = [{
            "provider": provider_id,
            "arm": "extractor",
            "case": "connect_or_run",
            "ok": False,
            "error": run_error,
            "elapsed": 0.0,
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
    payload = {
        "provider": provider_id,
        "arms": list(ARMS),
        "cases": [case.name for case in cases],
        "ok": _extractor_rows_ok(rows),
        "error": run_error,
        "rows": rows,
    }
    _write_report(output, payload)
    print(json.dumps({"ok": payload["ok"], "output": str(output)}, ensure_ascii=False))
    return 0 if payload["ok"] else 1


def _extractor_rows_ok(rows: list[dict[str, Any]]) -> bool:
    extractor_rows = [row for row in rows if row.get("arm") == "extractor"]
    return bool(extractor_rows) and all(bool(row.get("ok")) for row in extractor_rows)


if __name__ == "__main__":
    raise SystemExit(main())