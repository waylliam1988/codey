"""Build bounded project facts for one task before the Writer runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from codey.project_config import (
    ProjectConfigLoadResult,
    ProjectVerificationCommand,
    load_project_config,
    render_project_config_warnings,
)
from codey.project_facts import ProjectFactsStore, VerifiedCommand
from codey.knowledge.brief import KnowledgeBriefBuilder
from codey.knowledge.store import KnowledgeStore
from codey.project_map import MAX_PROJECT_MAP_CHARS, render_project_map
from codey.verification_policy import (
    VerificationCandidate,
    discover_verification_candidates,
    verification_candidate_lines,
)
from codey.work_checkpoint import (
    CheckpointCheck,
    WorkCheckpoint,
    WorkCheckpointStore,
    render_work_checkpoint,
)


@dataclass(frozen=True)
class CheckpointContext:
    item: WorkCheckpoint | None = None
    prompt: str = ""
    resumed: bool = False
    changed_files: tuple[str, ...] = ()
    successful_checks: tuple[VerificationCandidate, ...] = ()
    seed_checks: tuple[CheckpointCheck, ...] = ()
    resumed_verification_commands: tuple[CheckpointCheck, ...] = ()
    workspace_changed: bool = False


@dataclass(frozen=True)
class ProjectTaskContext:
    verified_facts: str = ""
    research_context: str = ""
    knowledge_context: str = ""
    project_map: str = ""
    project_config_warnings: str = ""
    verification_verified_commands: tuple[VerifiedCommand, ...] = ()
    resumed_verification_commands: tuple[CheckpointCheck, ...] = ()
    configured_verification_commands: tuple[ProjectVerificationCommand, ...] = ()
    configured_ignored_paths: tuple[str, ...] = ()
    project_map_chars: int = MAX_PROJECT_MAP_CHARS
    verification_candidates: tuple[VerificationCandidate, ...] = ()
    checkpoint: CheckpointContext = field(default_factory=CheckpointContext)


class ProjectTaskContextBuilder:
    """Prepare project facts and checkpoint context without running the Writer."""

    def __init__(
        self,
        *,
        project_facts: ProjectFactsStore | None = None,
        work_checkpoints: WorkCheckpointStore | None = None,
        knowledge_store: KnowledgeStore | None = None,
        config_result: ProjectConfigLoadResult | None = None,
    ) -> None:
        self.project_facts = project_facts
        self.work_checkpoints = work_checkpoints
        self.knowledge_store = knowledge_store
        self.config_result = config_result

    def build(
        self,
        *,
        project: str | Path,
        task: str,
        session_id: str,
        run_id: str,
        continue_task: bool,
        provider_session_changed: bool,
    ) -> ProjectTaskContext:
        # The caller may pass a pre-loaded config so one run reads
        # .codey/config.json exactly once.
        config_result = (
            self.config_result
            if self.config_result is not None
            else load_project_config(project)
        )
        config = config_result.config
        verified_facts = self._verified_facts(project)
        verified_commands = self._verified_commands(project)
        checkpoint = self._initialize_checkpoint(
            project=project,
            task=task,
            session_id=session_id,
            run_id=run_id,
            continue_task=continue_task,
            provider_session_changed=provider_session_changed,
        )
        verification_candidates = safe_verification_candidates(
            project,
            verified_commands,
            checkpoint.resumed_verification_commands,
            config.verification_commands,
            config.ignored_paths,
        )
        command_lines = verification_candidate_lines(verification_candidates)
        research_context = self._research_context(session_id)
        project_map_chars = _project_map_chars(config.context_budget_hints.project_map_chars)
        return ProjectTaskContext(
            verified_facts=verified_facts,
            research_context=research_context,
            knowledge_context=research_context,
            project_config_warnings=render_project_config_warnings(config_result),
            project_map=safe_project_map(
                project,
                verified_facts,
                task,
                command_lines,
                ignored_paths=config.ignored_paths,
                max_chars=project_map_chars,
            ),
            verification_verified_commands=verified_commands,
            resumed_verification_commands=checkpoint.resumed_verification_commands,
            configured_verification_commands=config.verification_commands,
            configured_ignored_paths=config.ignored_paths,
            project_map_chars=project_map_chars,
            verification_candidates=verification_candidates,
            checkpoint=checkpoint,
        )

    def refresh_checkpoint(self, item: WorkCheckpoint | None) -> CheckpointContext:
        if self.work_checkpoints is None or item is None:
            return CheckpointContext()
        try:
            return _checkpoint_context(
                self.work_checkpoints.reconcile(item),
                resumed=False,
            )
        except (OSError, ValueError):
            return CheckpointContext()

    def _verified_facts(self, project: str | Path) -> str:
        if self.project_facts is None:
            return ""
        try:
            return self.project_facts.render(project)
        except (OSError, ValueError):
            return ""

    def _verified_commands(self, project: str | Path) -> tuple[VerifiedCommand, ...]:
        if self.project_facts is None:
            return ()
        try:
            return self.project_facts.load(project).commands
        except (OSError, ValueError):
            return ()

    def _research_context(self, session_id: str) -> str:
        if self.knowledge_store is None:
            return ""
        try:
            return KnowledgeBriefBuilder(self.knowledge_store).build_for_session(session_id).render()
        except (OSError, ValueError):
            return ""

    def _initialize_checkpoint(
        self,
        *,
        project: str | Path,
        task: str,
        session_id: str,
        run_id: str,
        continue_task: bool,
        provider_session_changed: bool,
    ) -> CheckpointContext:
        if self.work_checkpoints is None:
            return CheckpointContext()
        try:
            previous = self.work_checkpoints.load(session_id)
            project_root = str(Path(project).expanduser().resolve())
            same_project = previous is not None and previous.project == project_root
            resume_requested = continue_task or provider_session_changed
            same_task = previous is not None and previous.original_task.strip() == task.strip()
            if same_project and resume_requested and (continue_task or same_task):
                return _checkpoint_context(
                    self.work_checkpoints.reconcile(previous),
                    resumed=True,
                )
            return CheckpointContext(
                item=self.work_checkpoints.start(
                    run_id=run_id,
                    session_id=session_id,
                    project=project,
                    task=task,
                )
            )
        except (OSError, ValueError):
            return CheckpointContext()


def _checkpoint_context(
    item: WorkCheckpoint,
    *,
    resumed: bool,
) -> CheckpointContext:
    checks = item.successful_checks_after_last_change
    return CheckpointContext(
        item=item,
        prompt=render_work_checkpoint(item),
        resumed=resumed,
        changed_files=tuple(changed.path for changed in item.changed_files),
        successful_checks=tuple(
            VerificationCandidate(check.command, check.cwd, "checkpoint")
            for check in checks
        ),
        seed_checks=checks,
        resumed_verification_commands=checks if resumed else (),
        workspace_changed=item.workspace_changed,
    )


def safe_verification_candidates(
    project: str | Path,
    verified_commands: tuple[VerifiedCommand, ...] = (),
    additional_commands: tuple[CheckpointCheck, ...] = (),
    configured_commands: tuple[ProjectVerificationCommand, ...] = (),
    ignored_paths: tuple[str, ...] = (),
) -> tuple[VerificationCandidate, ...]:
    try:
        return discover_verification_candidates(
            project,
            verified_commands + additional_commands,
            configured_commands=configured_commands,
            ignored_paths=ignored_paths,
        )
    except (OSError, TypeError, ValueError):
        return ()


def safe_project_map(
    project: str | Path,
    verified_facts: str,
    task: str = "",
    candidate_commands: tuple[str, ...] | None = None,
    ignored_paths: tuple[str, ...] = (),
    max_chars: int = MAX_PROJECT_MAP_CHARS,
) -> str:
    try:
        if candidate_commands is None:
            return render_project_map(
                project,
                verified_facts,
                task=task,
                ignored_paths=ignored_paths,
                max_chars=max_chars,
            )
        return render_project_map(
            project,
            verified_facts,
            task=task,
            candidate_commands=candidate_commands,
            ignored_paths=ignored_paths,
            max_chars=max_chars,
        )
    except Exception:
        return ""


def _project_map_chars(configured: int | None) -> int:
    if configured is None:
        return MAX_PROJECT_MAP_CHARS
    return min(MAX_PROJECT_MAP_CHARS, configured)
