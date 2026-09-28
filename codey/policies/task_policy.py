"""Persistent, explainable per-task authorization snapshot.

The policy is built once at the task entry boundary from user-visible intent
(task text, explicit capability flags, the Research button, the task kind) and
then treated as immutable. Model output (``TaskSubmission.model_hint``) can
never grant capabilities. Recovery reuses the stored policy and still passes
through the live path/command/network guards at execution time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TASK_POLICY_VERSION = 1

KNOWN_TASK_GRANTS = frozenset({
    "project.read",
    "project.write",
    "project.verify",
    "shell.approval",
    "web.read",
    "knowledge.read",
    "knowledge.write",
    "knowledge.link",
    "control",
})

# Canonical tool name -> required grant. Coding names use the model-visible
# names from toolchain definitions; research controller aliases map to the
# same web/knowledge grants as the tools they lower to.
CODING_TOOL_GRANTS: dict[str, str] = {
    "list_dir": "project.read",
    "read_file": "project.read",
    "read_files": "project.read",
    "grep": "project.read",
    "find_references": "project.read",
    "parallel": "project.read",
    "edit": "project.write",
    "run": "project.verify",
    "shell": "shell.approval",
    "done": "control",
}

RESEARCH_TOOL_GRANTS: dict[str, str] = {
    "web_search": "web.read",
    "open_url": "web.read",
    "source_search": "web.read",
    "open_result": "web.read",
    "reopen_source": "web.read",
    "open_hit": "web.read",
    "knowledge_search": "knowledge.read",
    "knowledge_read": "knowledge.read",
    "knowledge_write": "knowledge.write",
    "knowledge_link": "knowledge.link",
    "done": "control",
}

STRICT_RESEARCH_REQUIRED_CHECKS = (
    "research_sources_opened",
    "research_evidence_saved",
    "research_report_sections",
)


def _normalize_grant(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in KNOWN_TASK_GRANTS else ""


def _normalize_kind(value: object) -> str:
    return str(value or "").strip().lower()


def _has_project(submission: Any) -> bool:
    return bool(str(getattr(submission, "project", "") or "").strip())


def _requested_grants(submission: Any) -> frozenset[str]:
    raw = getattr(submission, "requested_capabilities", ())
    if isinstance(raw, str):
        raw = (raw,)
    try:
        items = tuple(raw or ())
    except TypeError:
        return frozenset()
    grants: set[str] = set()
    for item in items:
        grant = _normalize_grant(item)
        if grant:
            grants.add(grant)
    return frozenset(grants)


@dataclass(frozen=True)
class TaskPolicy:
    """Immutable authorization snapshot for one task run."""

    grants: frozenset[str]
    strict_research: bool = False
    required_checks: tuple[str, ...] = ()
    source: str = ""
    version: int = TASK_POLICY_VERSION

    def allows(self, grant: object) -> bool:
        return _normalize_grant(grant) in set(self.grants or frozenset())

    def to_payload(self) -> dict[str, object]:
        return {
            "grants": sorted(self.grants or frozenset()),
            "strict_research": bool(self.strict_research),
            "required_checks": list(self.required_checks or ()),
            "source": str(self.source or ""),
            "version": int(self.version or TASK_POLICY_VERSION),
        }

    @staticmethod
    def from_payload(payload: object) -> TaskPolicy:
        if not isinstance(payload, dict):
            return TaskPolicy(
                grants=frozenset({"control"}),
                source="recovered:invalid",
                version=TASK_POLICY_VERSION,
            )
        raw_grants = payload.get("grants", ())
        grants: set[str] = set()
        if isinstance(raw_grants, (list, tuple, set, frozenset)):
            for item in raw_grants:
                grant = _normalize_grant(item)
                if grant:
                    grants.add(grant)
        if "control" not in grants:
            grants.add("control")
        raw_checks = payload.get("required_checks", ())
        checks: list[str] = []
        if isinstance(raw_checks, (list, tuple)):
            for item in raw_checks:
                text = str(item or "").strip()
                if text and text not in checks:
                    checks.append(text)
        strict = payload.get("strict_research") is True
        source = str(payload.get("source") or "")
        try:
            version = int(payload.get("version") or TASK_POLICY_VERSION)
        except (TypeError, ValueError):
            version = TASK_POLICY_VERSION
        return TaskPolicy(
            grants=frozenset(grants),
            strict_research=strict,
            required_checks=tuple(checks[:16]),
            source=source[:240] if source else "recovered",
            version=version if version >= 1 else TASK_POLICY_VERSION,
        )


def build_task_policy(
    submission: Any,
    *,
    task_kind: object = "project",
    strict_research: object = False,
) -> TaskPolicy:
    """Build the immutable policy for one task from user intent only."""
    kind = _normalize_kind(task_kind)
    # The Research button is user intent; model_hint is never consulted here.
    strict = bool(strict_research is True) or bool(getattr(submission, "strict_research", False) is True)
    has_project = _has_project(submission)
    requested = _requested_grants(submission)

    grants: set[str] = {"control"}

    if kind in {"project", "hybrid"}:
        if has_project:
            grants.update({"project.read", "project.write", "project.verify", "shell.approval"})
        if "web.read" in requested:
            grants.add("web.read")
        if kind == "hybrid":
            # Hybrid intentionally mixes web and project tools in one run,
            # including saving research notes before writing code.
            grants.update({"web.read", "knowledge.read", "knowledge.write", "knowledge.link"})
        if strict:
            grants.update({"web.read", "knowledge.read", "knowledge.write", "knowledge.link"})
            if has_project:
                grants.update({"project.read", "project.verify"})
            if "project.write" in requested and has_project:
                grants.add("project.write")
    elif kind == "research":
        grants.update({"web.read", "knowledge.read", "knowledge.write", "knowledge.link"})
        if has_project:
            grants.update({"project.read", "project.verify"})
        if "project.write" in requested and has_project:
            grants.add("project.write")
    elif kind in {"planning", "planning_readonly", "readonly"}:
        if has_project:
            grants.add("project.read")
        if "web.read" in requested:
            grants.add("web.read")
        # Read-only planning never grants write/shell, even when requested.
        grants.discard("project.write")
        grants.discard("shell.approval")
    else:  # chat, review, unknown kinds: minimal surface.
        if "web.read" in requested:
            grants.add("web.read")

    if not has_project:
        grants.discard("project.read")
        grants.discard("project.write")
        grants.discard("project.verify")
        grants.discard("shell.approval")

    # Unknown requested capabilities are ignored (denied); they never widen grants.
    grants.intersection_update(KNOWN_TASK_GRANTS | {"control"})
    if "control" not in grants:
        grants.add("control")

    required_checks = STRICT_RESEARCH_REQUIRED_CHECKS if strict else ()
    requested_text = ",".join(sorted(requested)) if requested else "-"
    source = f"kind:{kind or 'project'};project:{'yes' if has_project else 'no'};requested:{requested_text};strict:{'yes' if strict else 'no'}"
    return TaskPolicy(
        grants=frozenset(grants),
        strict_research=strict,
        required_checks=tuple(required_checks),
        source=source,
        version=TASK_POLICY_VERSION,
    )


def visible_research_tools(
    policy: TaskPolicy | None,
    controller_allowed: tuple[str, ...] | list[str] | None,
) -> tuple[str, ...]:
    """Intersection of policy grants and controller state for research tools.

    The controller may only narrow research tools; it never widens policy and
    never governs project tools (see registry.snapshot_for_policy).
    """
    if policy is None:
        return ()
    allowed = {str(name or "").strip().lower() for name in (controller_allowed or ())}
    visible: list[str] = []
    for name in ("web_search", "open_url", "source_search", "knowledge_search", "knowledge_read",
                 "knowledge_write", "knowledge_link", "done"):
        grant = RESEARCH_TOOL_GRANTS.get(name, "")
        if grant and policy.allows(grant) and (not allowed or name in allowed or _alias_allowed(name, allowed)):
            visible.append(name)
    # Controller aliases lower to open_url; expose the alias the model must use
    # when the underlying web.read grant and controller state allow it.
    for alias in ("open_result", "reopen_source", "open_hit"):
        if alias in allowed and policy.allows("web.read") and alias not in visible:
            visible.append(alias)
    return tuple(visible)


def _alias_allowed(canonical: str, allowed: set[str]) -> bool:
    if canonical == "open_url":
        return bool({"open_result", "reopen_source", "open_hit"} & allowed)
    return False


__all__ = [
    "CODING_TOOL_GRANTS",
    "KNOWN_TASK_GRANTS",
    "RESEARCH_TOOL_GRANTS",
    "STRICT_RESEARCH_REQUIRED_CHECKS",
    "TASK_POLICY_VERSION",
    "TaskPolicy",
    "build_task_policy",
    "visible_research_tools",
]
