"""HTTP and task entry reject source IDs before submission or routing."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codey.app.api import run_submit_response
from codey.operations.task_run import prepare_submission
from codey.task.model import TaskSubmission


@pytest.mark.parametrize("value", [True, 42, [], None])
def test_http_rejects_nonstring_source(tmp_path, value):
    submit = Mock(return_value="r")
    status, _payload = run_submit_response({"task": "review", "project": str(tmp_path), "provider": "local",
        "intent": "review", "review_source_run_id": value}, submit)
    assert status == 400
    submit.assert_not_called()


def test_task_entry_rejects_nonstring_source():
    request = TaskSubmission("s", "p", "review", 4, False, "local", intent="review", review_source_run_id=True)
    with pytest.raises(ValueError):
        prepare_submission(SimpleNamespace(reserve_run=Mock(return_value=None)), request)
