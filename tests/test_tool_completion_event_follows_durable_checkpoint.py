"""An observed completed edit/run already has its continuation facts on disk."""
from types import SimpleNamespace

import pytest

from codey.operations.context import RunWork
from codey.operations.task_phases.hooks import _RunHookCallbacks
from codey.runs.work_checkpoint import WorkCheckpointStore
from codey.runtime.core.models import ToolCall
from codey.runtime.observe.events import RunEvent
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.toolchain.runtime import ToolOutcome
from codey.workspace.revision import WorkspaceRevisionStore


@pytest.mark.parametrize("tool", ["edit", "run"])
def test_completed_tool_is_checkpointed_before_external_observer_can_interrupt(tmp_path, tool):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "app.py"
    target.write_text("value = 1\n")
    store = WorkCheckpointStore(tmp_path / "state")
    item = store.start(run_id="r", session_id="s", project=str(project), task="Fix value")
    revisions = WorkspaceRevisionStore(tmp_path / "state")
    identity = revisions.current_state(project)
    work = RunWork([], ExecutionEvidence(workspace_revision=identity.revision, workspace_fingerprint=identity.fingerprint))
    work.work_checkpoint = item
    work.workspace_revision, work.workspace_fingerprint = identity.revision, identity.fingerprint
    if tool == "edit":
        target.write_text("value = 2\n")
    observed = []
    def emit(payload):
        checkpoint = store.load("s")
        assert checkpoint is not None
        if tool == "edit":
            assert [row.path for row in checkpoint.changed_files] == ["app.py"]
        else:
            assert checkpoint.successful_checks_after_last_change[0].command == "python -m unittest discover -v"
        observed.append(payload)
    hooks = _RunHookCallbacks(deps=SimpleNamespace(work_checkpoints=store, workspace_revisions=revisions),
        state=SimpleNamespace(emit=emit), work=work, session_id="s", run_id="r", project=str(project),
        max_turns=2, project_config_ignored=(), review_log_lines=10,
        project_completion_deps=SimpleNamespace(persistence=SimpleNamespace(project_facts=None)),
        current_provider_id=None, supervisor=None, self_repair=None)
    args = {"path": "app.py"} if tool == "edit" else {"command": "python -m unittest discover -v"}
    hooks.on_event(RunEvent.tool_finished(1, ToolCall(tool, args),
        ToolOutcome("finished", ok=True, changed=tool == "edit", exit_code=0)))
    assert len(observed) == 1
