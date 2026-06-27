from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codey.agent import run
from codey.providers.registry import connect_provider


def _make_fixture(root: Path, name: str) -> None:
    if name == "create":
        return
    if name == "edit":
        (root / "pricing.py").write_text(
            "def discounted_price(price, percent):\n"
            "    # LIVE_SMOKE_BUG\n"
            "    return price * (1 + percent / 100)\n",
            encoding="utf-8",
        )
        (root / "test_pricing.py").write_text(
            "import unittest\n\n"
            "from pricing import discounted_price\n\n\n"
            "class PricingTests(unittest.TestCase):\n"
            "    def test_discount(self):\n"
            "        self.assertEqual(discounted_price(100, 20), 80)\n",
            encoding="utf-8",
        )
        return
    raise ValueError(f"unknown fixture: {name}")


def run_smoke(provider_id: str, case: str, port: int, max_turns: int) -> dict:
    root = Path(tempfile.mkdtemp(prefix=f"codey-live-{case}-")).resolve()
    try:
        _make_fixture(root, case)
        events: list[str] = []

        def on_event(message: str) -> None:
            events.append(str(message))
            print(message)

        provider = connect_provider(provider_id, port=port)
        try:
            if case == "create":
                task = (
                    "Create math_utils.py with add(a, b) returning a + b. "
                    "Create test_math_utils.py with a unittest for add(2, 3) == 5. "
                    "Use edit with content to create the files, then run python -m unittest "
                    "and finish with done."
                )
            else:
                task = (
                    "Fix the LIVE_SMOKE_BUG in pricing.py by using search, read, edit, "
                    "then run python -m unittest and finish."
                )
            result = run(
                provider,
                root,
                task,
                max_turns=max_turns,
                on_event=on_event,
                fresh_chat=True,
            )
        finally:
            provider.close()
        return {
            "ok": result.stop_reason == "done",
            "summary": result.summary,
            "stop_reason": result.stop_reason,
            "turns": result.turns,
            "events": events,
            "project": str(root),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=("deepseek", "qwen", "mimo"), default="deepseek")
    ap.add_argument("--case", choices=("create", "edit"), default="create")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--max-turns", type=int, default=8)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    data = run_smoke(args.provider, args.case, args.port, args.max_turns)
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(data["summary"])
    return 0 if data["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
