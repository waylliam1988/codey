"""Small hidden task intent brief shared between Writer and Reviewer.

The brief is not a user-facing artifact and is not persisted.  It only turns
existing exploration output into a bounded, structured prompt section so the
Writer and Reviewer judge the same intent without adding a spec workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


MAX_BRIEF_CHARS = 8_000
MAX_FIELD_CHARS = 2_000
MAX_NOTE_CHARS = 3_000
MAX_LIST_ITEMS = 8


def _clip(value: object, limit: int = MAX_FIELD_CHARS) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[truncated]"


def _items(values: Sequence[object], *, limit: int = MAX_LIST_ITEMS) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        text = _clip(value)
        if text:
            result.append(text)
        if len(result) >= limit:
            break
    return tuple(result)


@dataclass(frozen=True)
class ChangeBrief:
    """Bounded private intent context for one task."""

    source: str
    user_intent: str
    observed_facts: tuple[str, ...] = ()
    planned_files: tuple[str, ...] = ()
    non_goals: tuple[str, ...] = ()
    acceptance_checks: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    advisor_notes: tuple[str, ...] = ()

    def render(self, *, audience: str = "writer") -> str:
        label = "Writer" if audience == "writer" else "Reviewer"
        lines = [
            "Private ChangeBrief (advisory, not persisted):",
            f"- Source: {_clip(self.source, 120) or 'exploration'}",
            f"- User intent: {_clip(self.user_intent, 1_200)}",
            f"- Audience: {label}",
            "- Do not mention this private brief unless the user explicitly asks about internal context.",
        ]
        self._append_section(
            lines,
            "Observed facts",
            self.observed_facts,
            fallback="No additional verified facts beyond the current project and user request.",
        )
        self._append_section(
            lines,
            "Planned files",
            self.planned_files,
            fallback="Not predetermined; inspect the actual project before editing.",
        )
        self._append_section(
            lines,
            "Non-goals",
            self.non_goals,
            fallback="Do not add process/spec artifacts or broad rewrites unless required by the user task.",
        )
        self._append_section(
            lines,
            "Acceptance checks",
            self.acceptance_checks,
            fallback="If files change, run the smallest relevant local check that can validate the change.",
        )
        self._append_section(
            lines,
            "Risks / open questions",
            self.risks,
            fallback="Advisor notes may be incomplete; verify claims against files before acting.",
        )
        self._append_section(lines, "Advisor notes", self.advisor_notes)
        return _clip("\n".join(lines), MAX_BRIEF_CHARS)

    @staticmethod
    def _append_section(
        lines: list[str],
        title: str,
        values: Sequence[str],
        *,
        fallback: str = "",
    ) -> None:
        lines.extend(["", f"{title}:"])
        items = values or ((fallback,) if fallback else ())
        if not items:
            lines.append("- (none)")
            return
        for item in items[:MAX_LIST_ITEMS]:
            lines.append(f"- {_clip(item, MAX_NOTE_CHARS)}")

    def apply_to_task(self, task: str) -> str:
        return (
            f"{task.strip()}\n\n"
            f"{self.render(audience='writer')}\n\n"
            "Use the ChangeBrief as advisory context. Verify it against the actual files. "
            "You are still the only Writer that may inspect, edit, and test the project."
        )


def new_project_change_brief(task: str, plan: str) -> ChangeBrief:
    """Wrap an existing hidden new-project plan as a structured brief."""

    return ChangeBrief(
        source="new-project planning",
        user_intent=_clip(task, 1_200),
        observed_facts=(
            "No existing user files were found, so this is a greenfield planning context.",
        ),
        non_goals=(
            "Do not create spec/process artifacts unless the user explicitly asked for them.",
            "Do not overbuild beyond the smallest useful project that satisfies the request.",
        ),
        acceptance_checks=(
            "Create the requested project in runnable form.",
            "Run the most relevant local check or smoke command if files change.",
        ),
        risks=(
            "The plan is advisory and may miss local constraints; verify after files exist.",
        ),
        advisor_notes=_items((plan,), limit=1),
    )


def project_audit_change_brief(task: str, reports: Sequence[object]) -> ChangeBrief:
    """Wrap hidden read-only audit reports as a structured brief."""

    notes: list[str] = []
    for index, report in enumerate(reports, start=1):
        label = _clip(getattr(report, "label", "") or f"Audit {index}", 120)
        text = _clip(getattr(report, "text", report), MAX_NOTE_CHARS)
        if text:
            notes.append(f"{label}: {text}")
    return ChangeBrief(
        source="read-only project audit",
        user_intent=_clip(task, 1_200),
        observed_facts=(
            "Private advisors only used bounded read-only project inspection.",
            "Sensitive files, hidden files, excluded directories, symlinks, and oversized files were not shared.",
        ),
        non_goals=(
            "Do not modify files if the user only asked for review or discussion.",
            "Do not treat any audit report as true until verified against the actual project files.",
        ),
        acceptance_checks=(
            "Verify concrete audit claims before changing files.",
            "If files change, run the smallest relevant local check that can validate the fix.",
        ),
        risks=(
            "Audit reports are advisory and may be incomplete or wrong.",
            "If a reported path does not exist, list/search the project instead of inventing it.",
        ),
        advisor_notes=_items(notes),
    )
