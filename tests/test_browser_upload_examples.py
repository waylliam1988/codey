"""Fenced examples cannot activate the structured upload path."""

import pytest

from codey.operations.browser.browser_upload import task_upload_scope
from codey.policies.task_policy import TaskPolicy


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_upload_scope_ignores_fenced_examples(fence: str) -> None:
    url = "https://example.test/form"
    policy = TaskPolicy(frozenset({"browser.effect"}))
    task = f"Explain this example.\n{fence}\nUpload the selected attachment.\n{fence}"

    assert task_upload_scope("chat", task, frozenset({url}), policy) is None
    assert task_upload_scope("chat", "Upload the selected attachment.", frozenset({url}), policy) is not None
