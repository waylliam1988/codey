"""Real desktop and CLI entries share automatic review and single repair."""
import json

import pytest

from codey.app import cli, task_submit
from codey.app.context import AppContext
from codey.app.headless_runner import HeadlessAppContext
from codey.env_names import REVIEW_POLICY_ENV
from tests.support.local_review_fixture import ScriptedLocal, fixture_project, writer_turns


@pytest.mark.usefixtures("no_external_advisor_models", "scripted_local_api_connection")
@pytest.mark.parametrize("finding", [False, True])
def test_desktop_and_real_cli_automatically_review_and_repair_equally(tmp_path, monkeypatch, capsys, finding):
    monkeypatch.setenv("NATIVE_TOOLS", "0")
    monkeypatch.setenv(REVIEW_POLICY_ENV, "web_if_available")
    monkeypatch.setattr("codey.app.review_service.providers.reviewer_candidates", lambda *_: ())
    results = []
    for entry in ("desktop", "cli"):
        root = tmp_path / entry
        root.mkdir()
        project = fixture_project(root)
        before = "def add(a, b):\n    return a - b\n"
        written = "def add(a, b):\n    return a + b\n"
        repaired = 'def add(a, b):\n    """Return the sum."""\n    return a + b\n'
        writer = ScriptedLocal("writer", writer_turns(before, written), [])
        repair = ScriptedLocal("repair", writer_turns(written, repaired), [])
        response = {"verdict": "approved", "summary": "Correct addition", "findings": []}
        if finding:
            response = {"verdict": "changes_requested", "summary": "Requested docstring missing", "findings": [
                {"path": "math_utils.py", "issue": "Requested docstring missing", "suggested_fix": "Add a docstring to add"}]}
        reviewer = ScriptedLocal("reviewer", [response], [])
        writers = iter((writer, repair))
        monkeypatch.setattr("codey.app.review_service.providers.connect_fresh_provider_tab", lambda *_, reviewer=reviewer: reviewer)
        task = "Fix add to return the sum and document it."
        if entry == "desktop":
            rows = []
            state = AppContext(root / "state")
            monkeypatch.setattr(state, "get_provider", lambda *_, writers=writers: next(writers))
            monkeypatch.setattr(state, "_on_event_emitted", rows.append)
            try:
                task_submit.run_task("s", str(project), task, 8, False, "local", "project",
                                     project_changes_required=True, get_state=lambda state=state: state)
            finally:
                state.close()
        else:
            monkeypatch.setattr(HeadlessAppContext, "get_provider", lambda *_, writers=writers: next(writers))
            exit_code = cli.main(["agent", "--provider", "local", "--project", str(project),
                                  "--state-home", str(root / "state"), "--max-turns", "8", "--json", task])
            assert exit_code == 0
            rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert reviewer.chats == 1, (entry, rows)
        assert len(reviewer.prompts) == 1
        assert len(repair.prompts) == (4 if finding else 0)
        assert (project / "math_utils.py").read_text() == (repaired if finding else written)
        terminal = [row for row in rows if row["type"] == "task_done"]
        assert len(terminal) == 1 and terminal[0]["stop_reason"] == "done"
        assert terminal[0]["review"]["status"] == "complete"
        results.append([(row.get("tool_name", row.get("tool")), row.get("ok")) for row in rows if row["type"] == "tool"])
    assert results[0] == results[1]
