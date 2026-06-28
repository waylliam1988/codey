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
from codey.changes import ChangeTracker
from codey.providers.registry import connect_provider
from codey.review import (
    has_reviewable_changes,
    parse_review_response,
    render_review_prompt,
    render_writer_followup,
)
from codey.server import collect_changes


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


def _run_review_pass(
    *,
    writer_id: str,
    reviewer_id: str,
    root: Path,
    task: str,
    writer_summary: str,
    changes: dict,
    events: list[str],
    port: int,
    max_turns: int,
    tracker: ChangeTracker,
) -> tuple[str, str, str]:
    reviewer = connect_provider(reviewer_id, port=port, open_if_missing=False, bring_to_front=False)
    try:
        reviewer.new_chat()
        reply = reviewer.send(
            render_review_prompt(
                project=str(root),
                task=task,
                writer_summary=writer_summary,
                changes=changes,
                recent_log="\n".join(events[-80:]),
            )
        )
        result = parse_review_response(reply)
    finally:
        reviewer.close()

    if result.approved:
        return "approved", writer_summary, "done"

    followup = render_writer_followup(task, result)
    writer = connect_provider(writer_id, port=port)
    try:
        fixed = run(
            writer,
            root,
            followup,
            max_turns=min(max_turns, 12),
            on_event=lambda message: events.append(str(message)) or print(message),
            fresh_chat=False,
            change_tracker=tracker,
        )
    finally:
        writer.close()
    return "changes_requested", fixed.summary, fixed.stop_reason


def run_smoke(
    provider_id: str,
    case: str,
    port: int,
    max_turns: int,
    reviewer_id: str | None = None,
) -> dict:
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
            tracker = ChangeTracker(root)
            result = run(
                provider,
                root,
                task,
                max_turns=max_turns,
                on_event=on_event,
                fresh_chat=True,
                change_tracker=tracker,
            )
            review_status = "not_run"
            final_summary = result.summary
            final_stop_reason = result.stop_reason
            if result.stop_reason == "done" and reviewer_id:
                changes = collect_changes(root, tracker)
                if has_reviewable_changes(changes):
                    provider.close()
                    provider = None
                    review_status, final_summary, final_stop_reason = _run_review_pass(
                        writer_id=provider_id,
                        reviewer_id=reviewer_id,
                        root=root,
                        task=task,
                        writer_summary=result.summary,
                        changes=changes,
                        events=events,
                        port=port,
                        max_turns=max_turns,
                        tracker=tracker,
                    )
        finally:
            if provider is not None:
                provider.close()
        return {
            "ok": final_stop_reason == "done",
            "summary": final_summary,
            "stop_reason": final_stop_reason,
            "turns": result.turns,
            "review": review_status,
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
    ap.add_argument("--reviewer", choices=("deepseek", "qwen", "mimo"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    data = run_smoke(args.provider, args.case, args.port, args.max_turns, args.reviewer)
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(data["summary"])
    return 0 if data["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
