"""Production submissions persist real Chat workspace actions and truthful details."""
from pathlib import Path

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.runs.details import load_run_details
from codey.runs.ledger import RunLedgerStore
from codey.runs.ledger_projection import load_run_projection
from codey.runs.trace import RunTraceStore
from codey.storage.local_store import session_key
from tests.support.workspace_conversation import TextConversation

pytestmark = pytest.mark.usefixtures("scripted_local_api_connection")


@pytest.mark.parametrize("attached", [False, True])
def test_chat_submission_records_edit_in_original_workspace_and_run_details(tmp_path, attached):
    root, home = tmp_path / "project", tmp_path / "state"
    root.mkdir()
    provider = TextConversation('{"tool":"edit","args":{"path":"result.txt","content":"actual"}}',
                                "Created result.txt.")
    rows = []
    result = run_headless(HeadlessRequest(project=root if attached else None, intent="auto",
                                         task="Create result.txt; do not run commands.", max_turns=2,
                                         provider_id="local", state_home=home),
                          emit_jsonl=rows.append, connect_provider=lambda *_, **__: provider)
    assert result.stop_reason == "done", rows
    workspace = root if attached else home / "workspaces" / session_key(result.session_id)
    assert (workspace / "result.txt").read_text(encoding="utf8") == "actual"
    projection = load_run_projection(RunLedgerStore(home), result.session_id, result.run_id)
    assert projection is not None
    assert Path(projection.project) == workspace
    assert projection.tool_counts.get("edit") == 1
    assert projection.final_changes.changed_count == 1
    details = load_run_details(run_ledgers=RunLedgerStore(home), run_traces=RunTraceStore(home),
                              session_id=result.session_id, run_id=result.run_id)
    assert any(row.label == "Actions" and "edited 1 file" in row.value for row in details.rows)
    assert len(provider.requests) == 2
