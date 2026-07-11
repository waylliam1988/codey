from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey import provider_controls
from codey.providers.registry import connect_provider
from tests.manual.project_map_symbol_ab import CASES, _prompt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--no-new-chat", action="store_true")
    parser.add_argument("--project", default=".")
    parser.add_argument("--case", choices=[case.name for case in CASES], default="")
    parser.add_argument("--arm", choices=("baseline", "project_map", "symbol_map"), default="baseline")
    parser.add_argument(
        "message",
        nargs="?",
        default='Return exactly {"ok":true} and no markdown.',
    )
    args = parser.parse_args()

    provider_controls.begin_task_context("qwen-submit-probe")
    provider = None
    try:
        provider = connect_provider("qwen", port=args.port)
        page = provider.session.page
        print(f"connected url={page.url}", flush=True)
        if not args.no_new_chat:
            started = time.time()
            provider.new_chat()
            print(f"new_chat seconds={time.time() - started:.2f} url={page.url}", flush=True)
        message = args.message
        if args.case:
            root = Path(args.project).expanduser().resolve()
            case = next(case for case in CASES if case.name == args.case)
            message = _prompt(case, root, arm=args.arm)
            print(f"probe_prompt case={args.case} arm={args.arm} chars={len(message)}", flush=True)
        started = time.time()
        try:
            reply = provider.send(message, timeout=args.timeout)
        finally:
            print(f"after send attempt seconds={time.time() - started:.2f} url={page.url}", flush=True)
            try:
                print("title=" + page.title(), flush=True)
            except Exception as exc:
                print(f"title error={type(exc).__name__}: {exc}", flush=True)
            try:
                print(
                    "counts "
                    f"textarea={page.locator('textarea.message-input-textarea').count()} "
                    f"send={page.locator('button.send-button').count()} "
                    f"stop={page.locator('button.stop-button').count()} "
                    f"responses={page.locator('.chat-response-message').count()}",
                    flush=True,
                )
            except Exception as exc:
                print(f"count error={type(exc).__name__}: {exc}", flush=True)
        print("reply=" + reply[:1000], flush=True)
        return 0
    finally:
        try:
            if provider is not None:
                provider.close()
        finally:
            provider_controls.end_task_context()


if __name__ == "__main__":
    raise SystemExit(main())
