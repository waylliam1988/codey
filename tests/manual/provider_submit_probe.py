from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey import deepseek, glm, mimo, provider_controls, qwen, stepfun
from codey.providers.registry import connect_fresh_provider_tab, connect_provider, provider_ids


PROVIDER_MODULES = {
    "deepseek": deepseek,
    "mimo": mimo,
    "qwen": qwen,
    "stepfun": stepfun,
    "glm": glm,
}

STATE_ACTIONS = (
    "message_box",
    "send_button",
    "stop_button",
    "idle_button",
    "response",
    "response_action",
    "copy_button",
    "regenerate_button",
)


def _page_state(provider_id: str, page) -> dict:
    module = PROVIDER_MODULES[provider_id]
    selectors_by_action = {
        action: list(module.PROFILE.selectors(action))
        for action in STATE_ACTIONS
        if module.PROFILE.selectors(action)
    }
    try:
        return page.evaluate(
            """
            ({selectorsByAction}) => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0
                  && s.visibility !== 'hidden' && s.display !== 'none';
              };
              const actions = {};
              for (const [action, selectors] of Object.entries(selectorsByAction)) {
                actions[action] = selectors.map((selector) => {
                  let all = [];
                  try {
                    all = Array.from(document.querySelectorAll(selector));
                  } catch (err) {
                    return {selector, error: String(err)};
                  }
                  return {
                    selector,
                    count: all.length,
                    visibleCount: all.filter(visible).length,
                  };
                });
              }
              return {
                url: location.href,
                title: document.title,
                actions,
              };
            }
            """,
            {"selectorsByAction": selectors_by_action},
        )
    except Exception as exc:
        return {"state_error": f"{type(exc).__name__}: {exc}"}


def main(argv: list[str] | None = None) -> int:
    choices = tuple(provider_id for provider_id in provider_ids() if provider_id != "local")
    parser = argparse.ArgumentParser(description="Live provider submit/idle smoke")
    parser.add_argument("--provider", choices=choices, default="stepfun")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--pad-chars", type=int, default=0)
    parser.add_argument(
        "message",
        nargs="?",
        default='Reply with exactly {"ok":true} and no markdown.',
    )
    args = parser.parse_args(argv)

    provider_controls.begin_task_context(f"{args.provider}-submit-probe")
    provider = None
    try:
        provider = (
            connect_fresh_provider_tab(args.provider, port=args.port)
            if args.fresh
            else connect_provider(args.provider, port=args.port)
        )
        page = provider.session.page
        print(
            "connected "
            + json.dumps(_page_state(args.provider, page), ensure_ascii=False),
            flush=True,
        )
        for index in range(max(1, args.rounds)):
            message = args.message
            if args.rounds > 1:
                message = (
                    f"{message}\n\n"
                    f"Round: {index + 1}. "
                    "This is a bounded submit probe; answer only the requested JSON."
                )
            if args.pad_chars > 0:
                message = message + "\n\nContext:\n" + ("probe context line. " * args.pad_chars)[
                    : args.pad_chars
                ]
            started = time.time()
            reply = provider.send(message, timeout=args.timeout)
            elapsed = time.time() - started
            print(
                f"after_send[{index + 1}] "
                + json.dumps(_page_state(args.provider, page), ensure_ascii=False),
                flush=True,
            )
            print(f"reply_seconds[{index + 1}]={elapsed:.2f}", flush=True)
            print(f"reply[{index + 1}]=" + reply[:1000], flush=True)
        return 0
    except Exception as exc:
        page = provider.session.page if provider is not None else None
        payload = {"error": f"{type(exc).__name__}: {exc}"}
        if page is not None:
            payload["page"] = _page_state(args.provider, page)
        print("failed " + json.dumps(payload, ensure_ascii=False), flush=True)
        return 1
    finally:
        if provider is not None and (not args.keep_open or not args.fresh):
            try:
                provider.close()
            except Exception:
                pass
        provider_controls.end_task_context()


if __name__ == "__main__":
    raise SystemExit(main())
