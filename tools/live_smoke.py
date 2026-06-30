from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codey.agent import run
from codey.changes import ChangeTracker
from codey.events import render_run_event
from codey.providers.registry import connect_provider
from codey.review import (
    has_reviewable_changes,
    parse_review_with_repair,
    render_review_prompt,
    render_writer_followup,
)
from codey.server import collect_changes


PROVIDER_IDS = ("deepseek", "qwen", "mimo")


def _record_event(events: list[str], event) -> None:
    text = render_run_event(event)
    events.append(text)
    print(text)


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


def _verify_fixture(root: Path, case: str) -> dict:
    """Verify the result independently of the model's own test command."""
    if case == "create":
        assertion = "from math_utils import add; assert add(2, 3) == 5"
    elif case == "edit":
        assertion = (
            "from pricing import discounted_price; "
            "assert discounted_price(100, 20) == 80"
        )
    else:
        raise ValueError(f"unknown fixture: {case}")

    commands = (
        [sys.executable, "-B", "-c", assertion],
        [sys.executable, "-B", "-m", "unittest"],
    )
    outputs: list[str] = []
    for command in commands:
        try:
            proc = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "exit_code": None,
                "output": "independent verification timed out after 120s",
            }
        output = "\n".join(
            part for part in (proc.stdout.rstrip(), proc.stderr.rstrip()) if part
        )
        if output:
            outputs.append(output)
        if proc.returncode != 0:
            return {
                "ok": False,
                "exit_code": proc.returncode,
                "output": "\n\n".join(outputs)[-4000:],
            }
    return {
        "ok": True,
        "exit_code": 0,
        "output": "\n\n".join(outputs)[-4000:],
    }


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
    reviewer = None
    try:
        reviewer = connect_provider(reviewer_id, port=port, open_if_missing=False, bring_to_front=False)
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
        result = parse_review_with_repair(reply, reviewer.send)
    except Exception as exc:
        events.append(f"[review] unavailable: {exc}")
        return "unavailable", writer_summary, "done"
    finally:
        if reviewer is not None:
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
            on_event=lambda event: _record_event(events, event),
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
    if reviewer_id and reviewer_id == provider_id:
        raise ValueError("reviewer must be different from provider")
    root = Path(tempfile.mkdtemp(prefix=f"codey-live-{case}-")).resolve()
    try:
        _make_fixture(root, case)
        events: list[str] = []

        def on_event(event) -> None:
            _record_event(events, event)

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
        verification = _verify_fixture(root, case)
        return {
            "ok": final_stop_reason == "done" and verification["ok"],
            "summary": final_summary,
            "stop_reason": final_stop_reason,
            "turns": result.turns,
            "review": review_status,
            "verification": verification,
            "events": events,
            "project": str(root),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run_matrix(
    case: str,
    port: int,
    max_turns: int,
    provider_ids: tuple[str, ...] = PROVIDER_IDS,
) -> dict:
    results = []
    for provider_id in provider_ids:
        try:
            data = run_smoke(provider_id, case, port, max_turns)
        except Exception as exc:
            data = {
                "ok": False,
                "summary": str(exc),
                "stop_reason": "error",
                "provider": provider_id,
            }
        data.setdefault("provider", provider_id)
        results.append(data)
    return {"ok": all(item.get("ok") for item in results), "results": results}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=(*PROVIDER_IDS, "all"), default="deepseek")
    ap.add_argument("--case", choices=("create", "edit"), default="create")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--max-turns", type=int, default=8)
    ap.add_argument("--reviewer", choices=("deepseek", "qwen", "mimo"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.provider == "all":
        if args.reviewer:
            ap.error("--reviewer cannot be used with --provider all")
        data = run_matrix(args.case, args.port, args.max_turns)
    else:
        data = run_smoke(args.provider, args.case, args.port, args.max_turns, args.reviewer)
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
    elif args.provider == "all":
        for item in data["results"]:
            status = "PASS" if item.get("ok") else "FAIL"
            print(f"{item['provider']}: {status} - {item.get('summary', '')}")
    else:
        print(data["summary"])
    return 0 if data["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
