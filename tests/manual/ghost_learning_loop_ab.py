"""Manual live A/B for Ghost post-turn learning loop.

This script intentionally runs one provider at a time. It verifies that the
learning extractor uses a fresh provider session, writes local state, and that
the next prompt can use the learned typed directive without leaking internal
names.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codey.ghost.directive import build_ghost_directive
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.learning_loop import GhostLearningLoop, GhostLearningTurn
from codey.ghost.store import GhostSignalStore
from codey.browser import PROVIDER_START_URLS
from codey.providers.registry import (
    PROVIDER_TYPES,
    connect_fresh_provider_tab,
    connect_provider,
    provider_ids,
)
from codey import provider_controls
from tests.manual.ghost_directive_ab import _model_visible_context_leaked


RESULTS_DIR = Path(__file__).resolve().parent / "results"
BASELINE_PROMPT = "请用自然语言解释为什么回归测试重要。"
LEARNING_TEXT = "以后请记住我的回答风格偏好：回答短一点，并且先给结论。"
NEGATIVE_TEXT = "你错了。"


class FakeProvider:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.sent: list[str] = []
        self.new_chat_calls = 0
        self.closed = False

    def new_chat(self, timeout: float | None = None) -> None:
        self.new_chat_calls += 1

    def send(self, text: str, timeout: float | None = None) -> str:
        self.sent.append(text)
        if "Local Context:" in text:
            return "结论：回归测试能防止旧功能被改坏。"
        if self.replies:
            return self.replies.pop(0)
        return "回归测试很重要，因为它能在代码变化后重新验证旧功能是否仍然正常。它还能帮助团队更快发现问题。"

    def close(self) -> None:
        self.closed = True


class _FreshBorrowedSession:
    def __init__(self, page: Any) -> None:
        self.page = page

    def close(self) -> None:
        try:
            self.page.close()
        except Exception:
            pass


def _learning_reply() -> str:
    return json.dumps(
        {
            "signals": [
                {
                    "kind": "style_preference",
                    "scope": "user",
                    "summary": "Prefer concise replies.",
                    "evidence_quote": "回答短一点",
                    "confidence": 0.94,
                    "metadata": {"conflict_key": "reply_length", "value_key": "concise"},
                },
                {
                    "kind": "style_preference",
                    "scope": "user",
                    "summary": "Prefer answer-first replies.",
                    "evidence_quote": "先给结论",
                    "confidence": 0.94,
                    "metadata": {"conflict_key": "reply_structure", "value_key": "answer_first"},
                },
            ]
        },
        ensure_ascii=False,
    )


def _no_signal_reply() -> str:
    return '{"signals":[]}'


def run_ab(
    *,
    provider_id: str,
    main_provider,
    learning_provider_factory: Callable[[str], Any],
    timeout: float,
    new_chat_timeout: float,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as td:
        state_home = Path(td)
        signal_store = GhostSignalStore(state_home)
        inbox_store = GhostInboxStore(state_home)
        hebbian_store = GhostHebbianStore(state_home)
        loop = GhostLearningLoop(
            signal_store=signal_store,
            inbox_store=inbox_store,
            hebbian_store=hebbian_store,
        )

        main_provider.new_chat(timeout=new_chat_timeout)
        baseline_started = time.time()
        baseline = main_provider.send(BASELINE_PROMPT, timeout=timeout)

        learning_result = loop.learn_from_turn(
            GhostLearningTurn(
                mode="chat",
                user_text=LEARNING_TEXT,
                assistant_text="好的。",
                session_id="manual-ab",
                run_id="manual-learning",
                provider_id=provider_id,
            ),
            provider_factory=learning_provider_factory,
            timeout=timeout,
            new_chat_timeout=new_chat_timeout,
        )
        directive = build_ghost_directive(hebbian_store, session_id="manual-ab")

        main_provider.new_chat(timeout=new_chat_timeout)
        directive_prompt = f"{directive.text}\n\n{BASELINE_PROMPT}" if directive.text else BASELINE_PROMPT
        directive_answer = main_provider.send(directive_prompt, timeout=timeout)

        negative_result = loop.learn_from_turn(
            GhostLearningTurn(
                mode="chat",
                user_text=NEGATIVE_TEXT,
                assistant_text="请给出更具体的更正。",
                session_id="manual-ab",
                run_id="manual-negative",
                provider_id=provider_id,
            ),
            provider_factory=learning_provider_factory,
            timeout=timeout,
            new_chat_timeout=new_chat_timeout,
        )
        rows = inbox_store.list_candidates()
        active_nodes = hebbian_store.list_nodes(status="active")

    directive_text = directive.text
    checks = {
        "learning_ok": learning_result.ok,
        "style_accepted": learning_result.accepted_count >= 1,
        "style_reinforced": learning_result.reinforced_count >= 1,
        "directive_has_concise": "reply length = concise" in directive_text,
        "directive_has_answer_first": "reply structure = answer first" in directive_text,
        "negative_not_accepted": negative_result.accepted_count == 0,
        "no_internal_leak": not any(
            _model_visible_context_leaked(text)
            for text in (baseline, directive_answer)
        ),
        "directive_answer_not_longer": len(directive_answer) <= max(len(baseline), 1) * 1.15,
    }
    return {
        "provider": provider_id,
        "ok": all(checks.values()),
        "checks": checks,
        "baseline": {
            "chars": len(baseline),
            "text": baseline,
        },
        "directive_answer": {
            "chars": len(directive_answer),
            "text": directive_answer,
        },
        "directive": directive.to_payload(),
        "learning_result": learning_result.to_event(run_id="manual-learning", session_id="manual-ab"),
        "negative_result": negative_result.to_event(run_id="manual-negative", session_id="manual-ab"),
        "candidate_count": len(rows),
        "active_node_count": len(active_nodes),
        "elapsed": round(time.time() - baseline_started, 3),
    }


def _connect_main_provider(provider_id: str, *, port: int, open_if_missing: bool):
    return connect_provider(provider_id, port=port, open_if_missing=open_if_missing)


def _fresh_tab_from_main_provider(provider_id: str, main_provider: Any):
    session = getattr(main_provider, "session", None)
    owner_page = getattr(session, "page", None)
    if owner_page is None:
        return None
    start_url = PROVIDER_START_URLS.get(provider_id)
    provider_type = PROVIDER_TYPES.get(provider_id)
    if not start_url or provider_type is None:
        return None
    page = owner_page.context.new_page()
    try:
        page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
        return provider_type(_FreshBorrowedSession(page))
    except Exception:
        try:
            page.close()
        except Exception:
            pass
        raise


def _learning_provider_factory(provider_id: str, *, port: int, main_provider: Any = None):
    if main_provider is not None:
        provider = _fresh_tab_from_main_provider(provider_id, main_provider)
        if provider is not None:
            return provider
    return connect_fresh_provider_tab(provider_id, port=port)


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _self_test() -> None:
    learning_replies = [_learning_reply(), _no_signal_reply()]

    def factory(_provider_id: str) -> FakeProvider:
        return FakeProvider([learning_replies.pop(0)])

    payload = run_ab(
        provider_id="fake",
        main_provider=FakeProvider([]),
        learning_provider_factory=factory,
        timeout=1,
        new_chat_timeout=1,
    )
    if not payload["ok"]:
        raise AssertionError(payload)
    print("self-test ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one-provider Ghost learning loop A/B")
    parser.add_argument("--provider", choices=provider_ids(), default="deepseek")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--new-chat-timeout", type=float, default=45.0)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--no-open-if-missing", action="store_true")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        _self_test()
        return 0

    provider_id = str(args.provider).strip().lower()
    output = args.output or RESULTS_DIR / f"ghost_learning_loop_{provider_id}.json"
    provider_controls.begin_task_context(f"ghost-learning-loop-ab:{provider_id}")
    provider = None
    try:
        provider = _connect_main_provider(
            provider_id,
            port=args.port,
            open_if_missing=not args.no_open_if_missing,
        )
        payload = run_ab(
            provider_id=provider_id,
            main_provider=provider,
            learning_provider_factory=lambda pid: _learning_provider_factory(
                pid,
                port=args.port,
                main_provider=provider,
            ),
            timeout=args.timeout,
            new_chat_timeout=args.new_chat_timeout,
        )
    except Exception as exc:
        payload = {
            "provider": provider_id,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
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
