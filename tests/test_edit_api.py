from unittest.mock import Mock

import pytest

from codey.app.api import run_submit_response
from codey.durable import CommitConflict
from codey.task.model import EditUserMessage


def body():
    return {"session_id": "chat", "provider": "deepseek", "input": {
        "kind": "edit_user_message", "text": "new question", "entry_id": "entry-1", "expected_revision": 7}}


def test_edit_uses_same_submission_boundary_and_typed_input():
    submit = Mock(return_value="run")
    status, result = run_submit_response(body(), submit_task=submit)
    assert status == 200 and result["run_id"] == "run"
    assert submit.call_args.args[2] == "new question"
    assert submit.call_args.args[4] is False
    assert submit.call_args.kwargs["input"] == EditUserMessage("entry-1", "new question", 7)


@pytest.mark.parametrize("field,value", [("entry_id", ""), ("expected_revision", True), ("expected_revision", -1), ("expected_revision", None)])
def test_edit_rejects_missing_or_invalid_identity(field, value):
    request = body()
    request["input"][field] = value
    submit = Mock()
    assert run_submit_response(request, submit_task=submit)[0] == 400
    submit.assert_not_called()


def test_edit_conflict_is_reviewable_not_an_internal_server_error():
    status, result = run_submit_response(body(), submit_task=Mock(side_effect=CommitConflict("Conversation changed")))
    assert status == 409
    assert result["code"] == "conversation_conflict"
