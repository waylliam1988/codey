from __future__ import annotations

import subprocess

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from tests.test_headless_real_kernel_lifecycle import ScriptedProvider

pytestmark = pytest.mark.usefixtures("scripted_local_api_connection")


def test_untracked_read_task_repairs_only_output_and_completes(tmp_path, monkeypatch):
    monkeypatch.setenv("NATIVE_TOOLS", "0")
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "a.txt").write_text("observed\n")
    provider = ScriptedProvider([
        {"tool": "read_file", "args": {"path": "a.txt"}},
        {"tool": "done", "args": {"summary": '```json\n{"text":"observed"}\n```'}},
        {"tool": "done", "args": {"summary": '{"text":"observed"}'}},
    ])
    rows = []
    result = run_headless(HeadlessRequest(project=project, task="Read a.txt. Do not change files. Return JSON.",
        provider_id="local", intent="project", output_format="json", max_turns=4, state_home=tmp_path / "state"),
        emit_jsonl=rows.append, connect_provider=lambda *a, **kw: provider)
    assert result.exit_code == 0, rows
    assert len(provider.prompts) == 3
    assert len([r for r in rows if r.get("type") == "tool" and r.get("tool_name") == "read_file"]) == 1
    assert not any(r.get("tool_name") == "run" for r in rows)
    assert next(r for r in rows if r.get("type") == "task_done")["summary"] == '{"text":"observed"}'
