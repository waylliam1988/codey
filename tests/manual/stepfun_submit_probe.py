from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey import provider_controls
from codey.providers.registry import connect_fresh_provider_tab, connect_provider
from codey.providers.web_drivers import stepfun


def _page_state(page) -> dict:
    selectors = stepfun.PROFILE.selectors("send_button")
    try:
        return page.evaluate(
            """
            ({selectors, responseSelector}) => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0
                  && s.visibility !== 'hidden' && s.display !== 'none';
              };
              const boxes = Array.from(document.querySelectorAll(
                'textarea.Publisher_textarea__pMX9t,textarea,input,[role="textbox"],[contenteditable="true"]'
              )).filter(visible).map((el) => ({
                tag: el.tagName,
                disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
                valueLen: 'value' in el ? String(el.value || '').length : null,
                valuePreview: 'value' in el ? String(el.value || '').slice(0, 180) : '',
                className: String(el.className || '').slice(0, 180),
              }));
              const send = selectors.map((selector) => ({
                selector,
                count: document.querySelectorAll(selector).length,
                visibleCount: Array.from(document.querySelectorAll(selector)).filter(visible).length,
              }));
              return {
                url: location.href,
                title: document.title,
                boxes,
                send,
                responses: document.querySelectorAll(responseSelector).length,
              };
            }
            """,
            {"selectors": list(selectors), "responseSelector": stepfun.PROFILE.selector("response")},
        )
    except Exception as exc:
        return {"state_error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Live StepFun submit smoke")
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
    args = parser.parse_args()

    provider_controls.begin_task_context("stepfun-submit-probe")
    provider = None
    try:
        provider = (
            connect_fresh_provider_tab("stepfun", port=args.port)
            if args.fresh
            else connect_provider("stepfun", port=args.port)
        )
        page = provider.session.page
        print("connected " + json.dumps(_page_state(page), ensure_ascii=False), flush=True)
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
                + json.dumps(_page_state(page), ensure_ascii=False),
                flush=True,
            )
            print(f"reply_seconds[{index + 1}]={elapsed:.2f}", flush=True)
            print(f"reply[{index + 1}]=" + reply[:1000], flush=True)
        return 0
    except Exception as exc:
        page = provider.session.page if provider is not None else None
        payload = {"error": f"{type(exc).__name__}: {exc}"}
        if page is not None:
            payload["page"] = _page_state(page)
        print("failed " + json.dumps(payload, ensure_ascii=False), flush=True)
        return 1
    finally:
        if provider is not None and not args.keep_open:
            try:
                provider.close()
            except Exception:
                pass
        provider_controls.end_task_context()


if __name__ == "__main__":
    raise SystemExit(main())
