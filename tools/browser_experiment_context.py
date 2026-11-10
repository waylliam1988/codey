"""Experimental browser action history presentation helpers."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

from codey.knowledge.model import canonical_json
from codey.operations.task_session import TaskSession
from codey.research.answer_context import _render_context as production_render_context
from codey.research.answer_contract import AnswerScope
from codey.research.ledger import ResearchLedger


def render_experiment_context(
    session: TaskSession,
    ledger: ResearchLedger | None,
    scope: AnswerScope,
    visible: tuple[tuple[str, str], ...],
) -> str:
    """Present experimental action receipts without changing production prompts."""
    text = production_render_context(session, ledger, scope, visible)
    history = [
        {
            "result_ref": identity,
            "tool": result.call.name,
            "args": result.call.args,
            "ok": result.ok,
            "observation": {
                key: value for key, value in observation.items()
                if key not in {"text", "elements"}
            },
        }
        for identity, result in session._memory_results.items()
        if isinstance((observation := result.canonical.get("browser_observation")), dict)
    ][-8:]
    if not history:
        return text
    heading = "Use the exact [eN] labels shown"
    history_text = (
        "Browser action history (settled receipts, not source evidence; old element refs are invalid):\n"
        + canonical_json(history)
    )
    return text.replace(heading, history_text + "\n\n" + heading, 1)


@contextmanager
def browser_action_context() -> Iterator[None]:
    with patch("codey.research.answer_context._render_context", render_experiment_context):
        yield
