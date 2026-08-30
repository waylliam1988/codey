"""Prompt-context assembly for the coding agent."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from codey.completion.repair_context import (
    CONTEXT_SOURCE_KEY as COMPLETION_REPAIR_CONTEXT_SOURCE_KEY,
    DEFAULT_REPAIR_CONTEXT_BUDGET_CHARS,
)
from codey.policies.permissions import allows_context_source
from codey.runtime.prompt_envelope import (
    PromptEnvelope,
    PromptEnvelopeSection,
    RenderedPromptSection,
)
from codey.toolchain.runtime import safe_join
from codey.workspace.context_epoch import context_source_ref
from codey.workspace.context_source import (
    ContextSource,
    RenderedContextSource,
    render_context_sources_with_metadata,
)
from codey.runs.work_checkpoint import MAX_WORK_CHECKPOINT_PROMPT_CHARS

PROJECT_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
MAX_PROJECT_INSTRUCTION_CHARS = 12000
PROJECT_INSTRUCTIONS_CONTEXT_BUDGET = (
    MAX_PROJECT_INSTRUCTION_CHARS * len(PROJECT_INSTRUCTION_FILES) + 1000
)
VERIFIED_FACTS_CONTEXT_BUDGET = 8000
RESEARCH_CONTEXT_BUDGET = 7000
PROJECT_MAP_CONTEXT_BUDGET = 12000
PROJECT_CONFIG_WARNINGS_CONTEXT_BUDGET = 1200
WORK_CHECKPOINT_CONTEXT_BUDGET = MAX_WORK_CHECKPOINT_PROMPT_CHARS
INITIAL_LISTING_CONTEXT_BUDGET = 4000
CODING_CURRENT_CONTEXT_BUDGET = 3000
GHOST_DIRECTIVE_CONTEXT_BUDGET = 900
GHOST_CONTINUITY_CONTEXT_BUDGET = 900


@dataclass(frozen=True)
class ProjectInstruction:
    name: str
    content: str
    truncated: bool = False


@dataclass(frozen=True)
class AgentContext:
    text: str
    sections: tuple[RenderedPromptSection, ...]
    sources: tuple[RenderedContextSource, ...]


def load_project_instructions(
    root: Path,
    *,
    max_chars: int = MAX_PROJECT_INSTRUCTION_CHARS,
) -> list[ProjectInstruction]:
    docs: list[ProjectInstruction] = []
    for name in PROJECT_INSTRUCTION_FILES:
        path = safe_join(root, name)
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        truncated = len(content) > max_chars
        if truncated:
            content = content[:max_chars].rstrip() + "\n\n[content truncated]"
        docs.append(ProjectInstruction(name=name, content=content, truncated=truncated))
    return docs


def format_project_instructions(docs: list[ProjectInstruction]) -> str:
    if not docs:
        return ""
    chunks = []
    for doc in docs:
        label = f"{doc.name} (truncated)" if doc.truncated else doc.name
        chunks.append(f"--- {label} ---\n{doc.content}")
    return "\n\n".join(chunks)


def render_completion_repair_sources(
    profile,
    completion_repair_context: str,
) -> tuple[RenderedContextSource, ...]:
    if not completion_repair_context:
        return ()
    if not allows_context_source(profile, COMPLETION_REPAIR_CONTEXT_SOURCE_KEY):
        return ()
    return render_context_sources_with_metadata((
        ContextSource(
            key=COMPLETION_REPAIR_CONTEXT_SOURCE_KEY,
            loader=lambda: completion_repair_context,
            budget=DEFAULT_REPAIR_CONTEXT_BUDGET_CHARS,
            freshness="after_tool_result",
            why_included="bounded failure facts from the previous completion proof",
            capability_id="completion_repair_context",
            admission_reason="after_tool_result",
        ),
    )).sources


def build_agent_context(
    *,
    project: Path,
    request_text: str,
    system_prompt_text: str,
    profile,
    list_directory: Callable[[Path, str], object],
    project_instructions: list[ProjectInstruction],
    project_facts: str,
    research_context: str,
    project_map: str,
    project_config_warnings: str,
    work_checkpoint: str,
    ghost_directive: str,
    ghost_continuity: str,
    completion_repair_context: str,
    include_ghost_directive: bool = True,
) -> AgentContext:
    def intro_source(
        key: str,
        loader: Callable[[], str],
        budget: int,
        why_included: str,
        *,
        heading: str = "",
        capability_id: str = "agent_runner",
    ) -> ContextSource:
        return ContextSource(
            key=key,
            loader=loader,
            budget=budget,
            freshness="run_start",
            why_included=why_included,
            heading=heading,
            capability_id=capability_id,
            admission_reason="run_start_assembly",
        )

    sources: list[ContextSource] = []
    if include_ghost_directive:
        sources.append(intro_source(
            "ghost_directive",
            lambda: ghost_directive,
            GHOST_DIRECTIVE_CONTEXT_BUDGET,
            "bounded local confirmed Ghost memory",
            capability_id="local_context",
        ))
        sources.append(intro_source(
            "ghost_continuity",
            lambda: ghost_continuity,
            GHOST_CONTINUITY_CONTEXT_BUDGET,
            "bounded local continuity projection",
            capability_id="local_context",
        ))
    sources.extend((
        intro_source(
            "project_instructions",
            lambda: format_project_instructions(project_instructions),
            PROJECT_INSTRUCTIONS_CONTEXT_BUDGET,
            "project instruction files from the project root",
            heading="Project instructions:",
        ),
        intro_source(
            "verified_facts",
            lambda: project_facts,
            VERIFIED_FACTS_CONTEXT_BUDGET,
            "successful local checks recorded for this project",
            heading="Verified project facts from successful local runs:",
        ),
        intro_source(
            "research_brief",
            lambda: research_context,
            RESEARCH_CONTEXT_BUDGET,
            "bounded research brief from the current chat",
        ),
        intro_source(
            "project_map",
            lambda: project_map,
            PROJECT_MAP_CONTEXT_BUDGET,
            "bounded local project map prepared before writing",
        ),
        intro_source(
            "project_config_warnings",
            lambda: project_config_warnings,
            PROJECT_CONFIG_WARNINGS_CONTEXT_BUDGET,
            "bounded project-local config warnings",
            heading="Project config warnings:",
        ),
        intro_source(
            "work_checkpoint",
            lambda: work_checkpoint,
            WORK_CHECKPOINT_CONTEXT_BUDGET,
            "bounded local checkpoint from a previous project run",
        ),
        intro_source(
            "initial_listing",
            lambda: str(getattr(list_directory(project, "."), "model_text", "")),
            INITIAL_LISTING_CONTEXT_BUDGET,
            "current top-level project listing",
            heading="Initial listing:",
        ),
        intro_source(
            COMPLETION_REPAIR_CONTEXT_SOURCE_KEY,
            lambda: completion_repair_context,
            DEFAULT_REPAIR_CONTEXT_BUDGET_CHARS,
            "bounded failure facts from the previous completion proof",
            capability_id="completion_repair_context",
        ),
    ))
    rendered_context = render_context_sources_with_metadata(
        source
        for source in sources
        if allows_context_source(profile, source.key)
    )
    context = rendered_context.text
    context_block = f"{context}\n\n" if context else ""
    request_context = (
        "Project workspace: use paths relative to the project root.\n"
        f"{context_block}"
        f"User task:\n{request_text}"
    )
    rendered = PromptEnvelope((
        PromptEnvelopeSection(
            name="coding_system_prompt",
            text=system_prompt_text,
            purpose="coding JSON tool protocol",
            freshness="run_start",
            source_refs=("protocol:json",),
        ),
        PromptEnvelopeSection(
            name="coding_request_context",
            text=request_context,
            purpose="project workspace, bounded local context, and current request",
            freshness="run_start",
            source_refs=(
                "project:workspace",
                "request:user_task",
                *(context_source_ref(source.key) for source in rendered_context.sources),
            ),
            budget=sum(source.budget for source in rendered_context.sources),
            truncated=any(source.truncated for source in rendered_context.sources),
        ),
    )).render()
    return AgentContext(
        text=rendered.text,
        sections=rendered.sections,
        sources=rendered_context.sources,
    )
