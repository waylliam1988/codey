"""Live A/B probe for the Research-to-Code handoff (Research Brief).

Compares the 0.4.9-style research brief (markdown sections re-parsed locally,
plus the raw "Synthesis excerpt") against the 0.4.10 refs-aware structured
brief. Both arms receive the same underlying synthesis note content, the
same fixture project, and the same Writer task; only the rendered handoff
differs. This is the release gate the roadmap requires for changing
Writer-visible research context text.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey import agent, provider_controls
from codey.events import RunEvent, render_run_event
from codey.knowledge.brief import KnowledgeBriefBuilder
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore
from codey.providers.registry import DEFAULT_PROVIDER_ID, connect_provider, provider_ids
from codey.text_budget import clip_middle


ARMS = ("baseline", "projection")
RESULTS_DIR = Path(__file__).resolve().parent / "results"
SESSION_ID = "research-to-code-ab"
TRAP_TOKEN = "ACME_LEDGER_V3_MIGRATION"
KEY_FORMULA_TOKEN = "(amount - discount)"
IGNORED_CHANGED_PARTS = frozenset({".pytest_cache", "__pycache__"})

SYNTHESIS_TITLE = "How should discounted_total treat discounts and tax?"
SYNTHESIS_SOURCES = ("https://example.com/billing",)

REPORT_BODY = (
    "## 结论\n"
    "- Discounts apply before tax: total = (amount - discount) * (1 + tax_rate). [1]\n"
    "- The function keeps the name discounted_total(amount, discount, tax_rate). [1]\n"
    f"- {TRAP_TOKEN} replaces flat-file ledgers next quarter.\n"
    "\n## 关键证据\n"
    "- [1 p.2] The official billing docs show the pre-tax discount example.\n"
    "\n## 反证与限制\n"
    "- 未找到强反证；rounding behavior is not specified.\n"
    "\n## 来源质量\n"
    "- [1] primary · official · fresh · example.com\n"
    "\n## 搜索覆盖\n"
    "- query: discount before tax formula\n"
    "\n## 来源\n"
    "[1] Billing API docs - https://example.com/billing\n"
)

RELATED_NOTE_ID = "20240101T000000-related-note-deadbeef"

TASK = (
    "Implement discounted_total(amount, discount, tax_rate) in pricing.py so "
    "test_pricing.py passes. Follow the documented formula from the research "
    "context. Do not add features beyond it. Run python -m pytest -q and finish."
)

FIXTURE_FILES = {
    "pytest.ini": "[pytest]\n",
    "pricing.py": (
        "def discounted_total(amount, discount, tax_rate):\n"
        "    # RESEARCH_BRIEF_AB_BUG: wrong order, applies tax first.\n"
        "    return amount * (1 + tax_rate) - discount\n"
    ),
    "test_pricing.py": (
        "from pricing import discounted_total\n\n\n"
        "def test_discount_before_tax():\n"
        "    assert discounted_total(100, 10, 0.2) == 108.0\n\n"
        "def test_no_discount_is_plain_tax():\n"
        "    assert discounted_total(50, 0, 0.25) == 62.5\n"
    ),
}


@dataclass(frozen=True)
class AbCase:
    name: str


CASES = (AbCase(name="discount-before-tax"),)


def _write_case(root: Path) -> None:
    for rel, content in FIXTURE_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _changed_files(root: Path) -> tuple[str, ...]:
    changed: list[str] = []
    original = dict(FIXTURE_FILES)
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        parts = Path(rel).parts
        if any(part in IGNORED_CHANGED_PARTS for part in parts):
            continue
        if original.get(rel) != path.read_text(encoding="utf-8"):
            changed.append(rel)
    return tuple(sorted(dict.fromkeys(changed)))


def _run_process(root: Path, command: tuple[str, ...]) -> bool:
    if shutil.which(command[0]) is None:
        return False
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _baseline_brief() -> str:
    """Re-render the 0.4.9-style brief locally (tool-only baseline arm)."""

    lines = [
        "Research context from this chat:",
        f"- synthesis_id: {SESSION_ID}-synthesis",
        f"- original_question: {SYNTHESIS_TITLE}",
    ]
    sections = _legacy_sections(REPORT_BODY)
    for title, items in (
        ("Key conclusions:", sections["conclusions"]),
        ("Evidence URLs:", SYNTHESIS_SOURCES),
        ("Citation map:", sections["citation_map"]),
        ("Evidence items:", sections["evidence_items"]),
        ("Counter-evidence / limitations:", sections["counterpoints"]),
        ("Source quality risks:", sections["source_quality_risks"]),
        ("Risks:", sections["risks"]),
        ("Open questions:", sections["open_questions"]),
        ("Related note ids:", (RELATED_NOTE_ID,)),
    ):
        if items:
            lines.append(title)
            lines.extend(f"- {item}" for item in items)
    excerpt, _truncated = clip_middle(REPORT_BODY, 3600)
    lines.extend(("", "Synthesis excerpt:", excerpt))
    lines.extend((
        "",
        "Use this as background only. Verify against project files before editing.",
    ))
    rendered, _truncated = clip_middle("\n".join(line for line in lines if line), 6000)
    return rendered


def _projection_brief() -> str:
    """Render the production 0.4.10 brief through knowledge/brief.py."""

    with tempfile.TemporaryDirectory() as td:
        store = KnowledgeStore(Path(td))
        try:
            note = KnowledgeNote.create(
                type="synthesis",
                title=SYNTHESIS_TITLE,
                body=REPORT_BODY,
                sources=list(SYNTHESIS_SOURCES),
                open_questions=["Should rounding be banker's rounding?"],
                session_id=SESSION_ID,
            )
            store.write_note(note)
            brief = KnowledgeBriefBuilder(store).build_for_session(SESSION_ID)
        finally:
            store.close()
    return brief.render()


_LEGACY_HEADINGS = {
    "conclusions": ("结论", "结论候选", "key conclusions", "conclusion"),
    "counterpoints": ("反证与限制", "反证", "counter-evidence", "counter", "limitations"),
    "citation_map": ("来源", "sources", "references"),
    "source_quality_risks": ("来源质量", "source quality"),
    "evidence_items": ("关键证据", "evidence", "evidence ledger"),
    "risks": ("风险", "risks"),
}


def _legacy_extract(body: str, headings: tuple[str, ...], limit: int = 5) -> tuple[str, ...]:
    heading_lowers = tuple(item.lower() for item in headings)
    out: list[str] = []
    in_section = False
    for line in str(body or "").splitlines():
        stripped = line.strip()
        lower = stripped.strip("#:： ").lower()
        if stripped.startswith("#") or stripped.endswith((":", "：")):
            if any(item in lower for item in heading_lowers):
                in_section = True
                continue
            if in_section and out:
                break
        if not in_section:
            continue
        if stripped.startswith(("- ", "* ")):
            out.append(stripped[2:].strip())
        elif stripped and len(stripped) < 220:
            out.append(stripped)
        if len(out) >= limit:
            break
    return tuple(out)


def _legacy_sources(body: str) -> tuple[str, ...]:
    rows = _legacy_extract(body, _LEGACY_HEADINGS["citation_map"], limit=16)
    if rows:
        return rows
    return tuple(
        stripped
        for line in str(body or "").splitlines()
        if (stripped := line.strip()).startswith("[") and "]" in stripped
    )[:16]


def _legacy_sections(body: str) -> dict[str, tuple[str, ...]]:
    return {
        "conclusions": _legacy_extract(body, _LEGACY_HEADINGS["conclusions"]),
        "counterpoints": _legacy_extract(body, _LEGACY_HEADINGS["counterpoints"]),
        "citation_map": _legacy_sources(body),
        "source_quality_risks": _legacy_extract(
            body, _LEGACY_HEADINGS["source_quality_risks"]
        ),
        "evidence_items": _legacy_extract(body, _LEGACY_HEADINGS["evidence_items"]),
        "risks": _legacy_extract(body, _LEGACY_HEADINGS["risks"]),
        "open_questions": ("Should rounding be banker's rounding?",),
    }


def _arm_briefs() -> dict[str, str]:
    return {"baseline": _baseline_brief(), "projection": _projection_brief()}


def score_arm(*, summary: str, changed_files: tuple[str, ...], root: Path) -> dict[str, Any]:
    """Deterministic scorer over one finished arm."""

    blob_parts = [str(summary or "")]
    for rel in changed_files:
        path = root / rel
        if path.is_file():
            blob_parts.append(path.read_text(encoding="utf-8"))
    blob = "\n".join(blob_parts)
    compact_blob = blob.replace(" ", "")
    return {
        "key_conclusion_applied": KEY_FORMULA_TOKEN.replace(" ", "") in compact_blob,
        "trap_misused": TRAP_TOKEN.casefold() in blob.casefold(),
    }


def _tool_events(events: list[RunEvent]) -> list[RunEvent]:
    return [event for event in events if event.kind == "tool" and event.call is not None]


def _protocol_errors(events: list[RunEvent]) -> int:
    return sum(
        1
        for event in events
        if event.kind == "status" and "rejected invalid tool request" in event.message
    )


class CountingProvider:
    def __init__(self, provider, *, timeout: float, new_chat_timeout: float) -> None:
        self.provider = provider
        self.timeout = timeout
        self.new_chat_timeout = new_chat_timeout
        self.prompts: list[str] = []
        self.name = getattr(provider, "name", "provider")
        self.id = getattr(provider, "id", "")

    def new_chat(self, timeout: float | None = None) -> object:
        return self.provider.new_chat(timeout=timeout or self.new_chat_timeout)

    def send(self, text: str, timeout: float | None = None) -> str:
        self.prompts.append(text)
        return self.provider.send(text, timeout=timeout or self.timeout)

    def close(self) -> None:
        self.provider.close()


class _ScriptedProvider:
    id = "self-test"
    name = "Self Test"

    def __init__(self, *replies: str) -> None:
        self.prompts: list[str] = []
        self.replies = list(replies)

    def new_chat(self, timeout: float | None = None) -> object:
        return object()

    def send(self, text: str) -> str:
        self.prompts.append(text)
        if not self.replies:
            raise AssertionError("scripted provider ran out of replies")
        return self.replies.pop(0)

    def close(self) -> None:
        return None


def _run_arm(
    provider,
    case: AbCase,
    arm: str,
    *,
    max_turns: int,
    briefs: dict[str, str] | None = None,
) -> dict[str, Any]:
    # Single-case probe; the case name only labels rows, and both arms share
    # one fixture + task so any delta comes from the rendered handoff.
    briefs = briefs or _arm_briefs()
    brief_text = briefs.get(arm, "")
    if not brief_text:
        raise SystemExit(f"unknown arm: {arm}")
    events: list[RunEvent] = []
    started = time.time()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        _write_case(root)
        prompts_before = len(provider.prompts)
        result = agent.run(
            provider,
            root,
            TASK,
            max_turns=max_turns,
            on_event=events.append,
            fresh_chat=True,
            provider_id=getattr(provider, "id", ""),
            research_context=brief_text,
        )
        elapsed = round(time.time() - started, 3)
        changed = _changed_files(root)
        independent_check = _run_process(root, (sys.executable, "-B", "-m", "pytest", "-q"))
        scores = score_arm(
            summary=str(getattr(result, "summary", "")),
            changed_files=changed,
            root=root,
        )

    prompts = provider.prompts[prompts_before:]
    report: dict[str, Any] = {
        "case": case.name,
        "arm": arm,
        "seconds": elapsed,
        "stop_reason": result.stop_reason,
        "turns": result.turns,
        "tool_calls": len(_tool_events(events)),
        "protocol_errors": _protocol_errors(events),
        "sent_chars": sum(len(prompt) for prompt in prompts),
        "brief_chars": len(brief_text),
        "brief_has_raw_excerpt": "Synthesis excerpt" in brief_text,
        "brief_has_related_ids": RELATED_NOTE_ID in brief_text,
        "brief_has_trap_claim": TRAP_TOKEN in brief_text,
        "changed_files": list(changed),
        "independent_check_passed": independent_check,
        **scores,
        "success": bool(
            result.stop_reason == "done"
            and independent_check
            and scores["key_conclusion_applied"]
            and not scores["trap_misused"]
        ),
    }
    if not report["success"]:
        report["event_tail"] = [render_run_event(event) for event in events[-10:]]
    return report


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"arms": {}}
    for arm in ARMS:
        arm_rows = [row for row in rows if row.get("arm") == arm and "error" not in row]
        summary["arms"][arm] = {
            "count": len(arm_rows),
            "success": sum(1 for row in arm_rows if row.get("success")),
            "key_conclusion_applied": sum(
                1 for row in arm_rows if row.get("key_conclusion_applied")
            ),
            "trap_misused": sum(1 for row in arm_rows if row.get("trap_misused")),
            "independent_check_passed": sum(
                1 for row in arm_rows if row.get("independent_check_passed")
            ),
            "sent_chars": sum(int(row.get("sent_chars") or 0) for row in arm_rows),
            "brief_chars": sum(int(row.get("brief_chars") or 0) for row in arm_rows),
            "turns": sum(int(row.get("turns") or 0) for row in arm_rows),
            "tool_calls": sum(int(row.get("tool_calls") or 0) for row in arm_rows),
            "protocol_errors": sum(int(row.get("protocol_errors") or 0) for row in arm_rows),
        }
    baseline = summary["arms"].get("baseline", {})
    projection = summary["arms"].get("projection", {})
    if baseline.get("count") and projection.get("count"):
        summary["projection_delta_vs_baseline"] = {
            key: int(projection.get(key) or 0) - int(baseline.get(key) or 0)
            for key in (
                "success",
                "key_conclusion_applied",
                "trap_misused",
                "independent_check_passed",
                "sent_chars",
                "brief_chars",
                "turns",
                "tool_calls",
                "protocol_errors",
            )
        }
    return summary


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _default_output(provider_id: str) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return RESULTS_DIR / f"research_to_code_ab-{provider_id}-{stamp}.json"


def run_live(
    *,
    provider_id: str,
    port: int,
    timeout: float,
    new_chat_timeout: float,
    repeats: int,
    max_turns: int,
    output: Path,
    keep_open: bool,
) -> int:
    payload: dict[str, Any] = {
        "probe": "research_to_code_ab",
        "provider": provider_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "cases": [case.name for case in CASES],
        "arms": list(ARMS),
        "repeats_per_arm": repeats,
        "rows": [],
        "summary": {},
    }
    _atomic_write_json(output, payload)
    briefs = _arm_briefs()
    provider_controls.begin_task_context(f"research-to-code-ab:{provider_id}")
    provider_obj = None
    try:
        provider_obj = CountingProvider(
            connect_provider(provider_id, port=port),
            timeout=timeout,
            new_chat_timeout=new_chat_timeout,
        )
        for case in CASES:
            for arm in ARMS:
                for index in range(max(1, int(repeats))):
                    try:
                        row = _run_arm(
                            provider_obj,
                            case,
                            arm,
                            max_turns=max_turns,
                            briefs=briefs,
                        )
                        row["repeat"] = index + 1
                    except Exception as exc:
                        row = {
                            "case": case.name,
                            "arm": arm,
                            "repeat": index + 1,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    payload["rows"].append(row)
                    payload["summary"] = _summarize(payload["rows"])
                    _atomic_write_json(output, payload)
                    print(json.dumps(row, ensure_ascii=False), flush=True)
        payload["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        payload["summary"] = _summarize(payload["rows"])
        _atomic_write_json(output, payload)
        print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
        print(f"report: {output}")
        return 0 if all("error" not in row for row in payload["rows"]) else 1
    finally:
        try:
            if provider_obj is not None and not keep_open:
                provider_obj.close()
        finally:
            provider_controls.end_task_context()


def _self_test() -> None:
    briefs = _arm_briefs()
    assert "Synthesis excerpt" in briefs["baseline"]
    assert "Synthesis excerpt" not in briefs["projection"]
    assert RELATED_NOTE_ID in briefs["baseline"]
    assert RELATED_NOTE_ID not in briefs["projection"]
    for arm in ARMS:
        assert TRAP_TOKEN in briefs[arm], arm
        assert KEY_FORMULA_TOKEN.replace(" ", "") in briefs[arm].replace(" ", ""), arm

    replies = (
        '{"tool":"read_file","args":{"path":"pricing.py"}}',
        json.dumps({
            "tool": "edit",
            "args": {
                "path": "pricing.py",
                "replacements": [{
                    "old_string": (
                        "def discounted_total(amount, discount, tax_rate):\n"
                        "    # RESEARCH_BRIEF_AB_BUG: wrong order, applies tax first.\n"
                        "    return amount * (1 + tax_rate) - discount"
                    ),
                    "new_string": (
                        "def discounted_total(amount, discount, tax_rate):\n"
                        "    return (amount - discount) * (1 + tax_rate)"
                    ),
                }],
            },
        }),
        '{"tool":"run","args":{"command":"python -B -m pytest -q"}}',
        '{"tool":"done","args":{"summary":"Implemented pre-tax discount per research."}}',
        '{"tool":"done","args":{"summary":"Implemented pre-tax discount per research."}}',
    )
    case = CASES[0]
    for arm in ARMS:
        row = _run_arm(
            _ScriptedProvider(*replies),
            case,
            arm,
            max_turns=6,
            briefs=briefs,
        )
        assert row["stop_reason"] == "done", row
        assert row["key_conclusion_applied"], row
        assert not row["trap_misused"], row
        assert row["independent_check_passed"], row
        assert row["success"], row
    baseline_row = _run_arm(_ScriptedProvider(*replies), case, "baseline", max_turns=6, briefs=briefs)
    projection_row = _run_arm(_ScriptedProvider(*replies), case, "projection", max_turns=6, briefs=briefs)
    assert baseline_row["brief_chars"] > projection_row["brief_chars"]
    print("self-test ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Live A/B: 0.4.9-style vs structured Research Brief handoff."
    )
    parser.add_argument("--provider", choices=provider_ids(), default=DEFAULT_PROVIDER_ID)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--new-chat-timeout", type=float, default=45.0)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        _self_test()
        return 0
    output = args.output or _default_output(args.provider)
    return run_live(
        provider_id=args.provider,
        port=args.port,
        timeout=args.timeout,
        new_chat_timeout=args.new_chat_timeout,
        repeats=args.repeats,
        max_turns=args.max_turns,
        output=output,
        keep_open=args.keep_open,
    )


if __name__ == "__main__":
    raise SystemExit(main())
