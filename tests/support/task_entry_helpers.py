"""Real domain fixtures for the task entry boundary."""

from codey.agents.handoff import ConversationContext, ConversationSnapshot
from codey.operations.context import RunFrame
from codey.task.model import TaskSubmission


def submission(task: str = "inspect local code", *, project: str | None = "/tmp/project") -> TaskSubmission:
    return TaskSubmission(
        session_id="s", project=project, task=task, max_turns=8,
        continue_task=False, provider_id="local", run_id="r",
    )


def entry_frame(request: TaskSubmission) -> RunFrame:
    return RunFrame(
        request=request, run_id=request.run_id, task_kind="project",
        provider=None, provider_id=request.provider_id,
        project_text=request.project or "", conversation=ConversationContext(),
        fresh_chat=False, handoff="", research_handoff="",
        prior_snapshot=ConversationSnapshot(mode="project"), recovered_owner_prompt="",
        provider_session_changed=False, preflight_tried=set(), preflight_switches=0,
    )
