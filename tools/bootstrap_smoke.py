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


BUGGY_DIFF = (
    "def _diff_for(path: str, before: str | None, after: str | None) -> str:\n"
    "    before_lines = [] if before is None else before.splitlines(keepends=True)\n"
    "    after_lines = [] if after is None else after.splitlines(keepends=True)\n"
    "    fromfile = \"/dev/null\" if before is None else f\"a/{path}\"\n"
    "    tofile = \"/dev/null\" if after is None else f\"b/{path}\"\n"
    "    diff = difflib.unified_diff(after_lines, before_lines, fromfile=fromfile, tofile=tofile, lineterm=\"\")\n"
    "    body = \"\\n\".join(diff)\n"
    "    return f\"diff --git a/{path} b/{path}\\n{body}\" if body else \"\"\n"
)


BOOTSTRAP_TASK = (
    "This is a Codey self-repair test. First run "
    "python -B -m unittest tests.test_changes and inspect the failure. "
    "Fix the bug in codey/changes.py only. Then run "
    "python -B -m unittest tests.test_changes again. Use JSON tools only. "
    "When tests are green, finish with done and summarize the fix."
)


def _record_event(events: list[str], event) -> None:
    text = render_run_event(event)
    events.append(text)
    print(text)


def _copy_repo(src: Path, dst: Path) -> None:
    ignore = shutil.ignore_patterns(
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        "dist",
        "build",
    )
    shutil.copytree(src, dst, ignore=ignore)


def inject_bug(root: Path) -> None:
    path = root / "codey" / "changes.py"
    text = path.read_text(encoding="utf-8")
    start = text.index("def _diff_for(")
    end = text.index("\n\nclass ChangeTracker:", start)
    path.write_text(text[:start] + BUGGY_DIFF + text[end:], encoding="utf-8")


def _run_python_unittest(root: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "unittest"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    output = "\n".join(part for part in [proc.stdout.rstrip(), proc.stderr.rstrip()] if part)
    return {"exit_code": proc.returncode, "output": output[-4000:]}


def _review_pass(
    *,
    writer_id: str,
    reviewer_id: str,
    port: int,
    root: Path,
    writer_summary: str,
    changes: dict,
    events: list[str],
    tracker: ChangeTracker,
    max_turns: int,
) -> tuple[str, str, str]:
    reviewer = None
    try:
        reviewer = connect_provider(reviewer_id, port=port, open_if_missing=False, bring_to_front=False)
        reviewer.new_chat()
        prompt = render_review_prompt(
            project=str(root),
            task=BOOTSTRAP_TASK,
            writer_summary=writer_summary,
            changes=changes,
            recent_log="\n".join(events[-80:]),
        )
        review = parse_review_with_repair(reviewer.send(prompt), reviewer.send)
    except Exception as exc:
        events.append(f"[review] unavailable: {exc}")
        return "unavailable", writer_summary, "done"
    finally:
        if reviewer is not None:
            reviewer.close()

    if review.approved:
        return "approved", writer_summary, "done"

    followup = render_writer_followup(BOOTSTRAP_TASK, review)
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


def run_bootstrap_smoke(
    provider_id: str,
    *,
    port: int,
    max_turns: int,
    reviewer_id: str | None = None,
    keep: bool = False,
) -> dict:
    if reviewer_id and reviewer_id == provider_id:
        raise ValueError("reviewer must be different from provider")

    repo = Path(__file__).resolve().parents[1]
    root = Path(tempfile.mkdtemp(prefix="codey-bootstrap-")).resolve()
    project = root / "repo"
    try:
        _copy_repo(repo, project)
        inject_bug(project)
        before = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "tests.test_changes"],
            cwd=project,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )

        events: list[str] = []

        def on_event(event) -> None:
            _record_event(events, event)

        tracker = ChangeTracker(project)
        provider = connect_provider(provider_id, port=port)
        try:
            result = run(
                provider,
                project,
                BOOTSTRAP_TASK,
                max_turns=max_turns,
                on_event=on_event,
                fresh_chat=True,
                change_tracker=tracker,
            )
            review_status = "not_run"
            final_summary = result.summary
            final_stop_reason = result.stop_reason
            if result.stop_reason == "done" and reviewer_id:
                changes = collect_changes(project, tracker)
                if has_reviewable_changes(changes):
                    provider.close()
                    provider = None
                    review_status, final_summary, final_stop_reason = _review_pass(
                        writer_id=provider_id,
                        reviewer_id=reviewer_id,
                        port=port,
                        root=project,
                        writer_summary=result.summary,
                        changes=changes,
                        events=events,
                        tracker=tracker,
                        max_turns=max_turns,
                    )
        finally:
            if provider is not None:
                provider.close()

        final_tests = _run_python_unittest(project)
        initial_failure = before.returncode != 0
        ok = initial_failure and final_stop_reason == "done" and final_tests["exit_code"] == 0
        return {
            "ok": ok,
            "provider": provider_id,
            "reviewer": reviewer_id,
            "review": review_status,
            "summary": final_summary,
            "stop_reason": final_stop_reason,
            "turns": result.turns,
            "initial_failure": initial_failure,
            "final_tests": final_tests,
            "events": events,
            "project": str(project),
        }
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=("deepseek", "qwen", "mimo"), default="deepseek")
    ap.add_argument("--reviewer", choices=("deepseek", "qwen", "mimo"))
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--max-turns", type=int, default=16)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    data = run_bootstrap_smoke(
        args.provider,
        port=args.port,
        max_turns=args.max_turns,
        reviewer_id=args.reviewer,
        keep=args.keep,
    )
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(data["summary"])
    return 0 if data["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
