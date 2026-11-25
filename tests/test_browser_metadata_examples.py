"""Example code fences do not request a browser metadata report."""

import pytest

from codey.operations.browser.browser_text_report import report_scope
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_metadata_report_ignores_fenced_examples(fence: str) -> None:
    session = TaskSession(
        TaskPolicy(frozenset({"browser.effect", "browser.interact"})), task_kind="chat",
        task_text=(f"Explain this example.\n{fence}\n"
                   "Use browser_eval to read window.exampleLabel and browser_tab_list for the tab.\n"
                   f"Report the sample token and URL.\n{fence}"),
    )

    assert report_scope(session) is None
