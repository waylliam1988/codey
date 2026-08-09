"""Internal permission profiles for Codey runtime phases.

Profiles describe the tools and context sources a phase should receive. They
do not replace runtime safety checks in tool_runtime, Research contracts, shell
approval, safe path validation, or run allowlists.
"""

from __future__ import annotations

from dataclasses import dataclass


KNOWN_CONTEXT_SOURCE_KEYS = frozenset({
    "ghost_directive",
    "project_instructions",
    "verified_facts",
    "research_brief",
    "project_map",
    "project_config_warnings",
    "work_checkpoint",
    "initial_listing",
    "coding_current_context",
})
CODING_WRITER_CONTEXT_SOURCE_KEYS = (
    "project_instructions",
    "verified_facts",
    "research_brief",
    "project_map",
    "project_config_warnings",
    "work_checkpoint",
    "initial_listing",
    "coding_current_context",
)
PLANNING_READONLY_CONTEXT_SOURCE_KEYS = (
    "ghost_directive",
    "project_instructions",
    "verified_facts",
    "research_brief",
    "project_map",
    "project_config_warnings",
    "work_checkpoint",
    "initial_listing",
    "coding_current_context",
)

KNOWN_REVIEW_CONTEXT_SOURCE_KEYS = frozenset({
    "task",
    "change_brief",
    "diff",
    "recent_log",
    "execution_evidence",
    "project_map",
    "verification_map",
    "review_impact_map",
})


@dataclass(frozen=True)
class PermissionProfile:
    name: str
    coding_permissions: tuple[str, ...] = ()
    coding_tools: tuple[str, ...] = ()
    research_tools: tuple[str, ...] = ()
    context_sources: tuple[str, ...] = ()
    review_context_sources: tuple[str, ...] = ()
    project_read: bool = False
    project_write: bool = False
    can_request_shell: bool = False
    user_visible: bool = False


PERMISSION_PROFILES = {
    "chat": PermissionProfile(
        name="chat",
    ),
    "research": PermissionProfile(
        name="research",
        research_tools=(
            "knowledge_search",
            "knowledge_read",
            "knowledge_write",
            "knowledge_link",
            "web_search",
            "open_url",
            "source_search",
            "done",
        ),
        project_read=False,
        project_write=False,
    ),
    "coding_writer": PermissionProfile(
        name="coding_writer",
        coding_permissions=(
            "project_read",
            "project_write",
            "project_verify",
            "user_approved_shell",
            "control",
        ),
        context_sources=CODING_WRITER_CONTEXT_SOURCE_KEYS,
        project_read=True,
        project_write=True,
        can_request_shell=True,
    ),
    "reviewer": PermissionProfile(
        name="reviewer",
        review_context_sources=(
            "task",
            "change_brief",
            "diff",
            "recent_log",
            "execution_evidence",
            "project_map",
            "verification_map",
            "review_impact_map",
        ),
        project_read=True,
        project_write=False,
    ),
    "planning_readonly": PermissionProfile(
        name="planning_readonly",
        coding_permissions=(
            "project_read",
            "control",
        ),
        coding_tools=(
            "list_dir",
            "read_file",
            "read_files",
            "grep",
            "find_references",
            "parallel",
            "done",
        ),
        context_sources=PLANNING_READONLY_CONTEXT_SOURCE_KEYS,
        project_read=True,
        project_write=False,
    ),
}


def profile_for_name(name: str) -> PermissionProfile:
    key = str(name or "coding_writer").strip().lower()
    try:
        return PERMISSION_PROFILES[key]
    except KeyError as exc:
        raise ValueError(f"unknown permission profile: {name}") from exc


def profile_for_task_kind(task_kind: str, *, phase: str = "") -> PermissionProfile:
    kind = str(task_kind or "").strip().lower()
    current_phase = str(phase or "").strip().lower()
    if current_phase in {"research", "hybrid_research"}:
        return profile_for_name("research")
    if current_phase in {"review", "reviewer"}:
        return profile_for_name("reviewer")
    if current_phase in {"planning", "readonly"}:
        return profile_for_name("planning_readonly")
    if current_phase in {"writer", "project_writer", "hybrid_writer"}:
        return profile_for_name("coding_writer")
    if kind == "research":
        return profile_for_name("research")
    if kind in {"project", "hybrid"}:
        return profile_for_name("coding_writer")
    if kind == "chat":
        return profile_for_name("chat")
    return profile_for_name("coding_writer")


def allowed_coding_tool_names(profile: PermissionProfile | str) -> tuple[str, ...]:
    current = profile_for_name(profile) if isinstance(profile, str) else profile
    if current.coding_tools:
        return current.coding_tools
    if not current.coding_permissions:
        return ()
    from codey.tool_definition import definitions_for_permissions

    return tuple(definition.name for definition in definitions_for_permissions(current.coding_permissions))


def allows_context_source(profile: PermissionProfile | str, key: str) -> bool:
    current = profile_for_name(profile) if isinstance(profile, str) else profile
    return str(key or "") in set(current.context_sources)
