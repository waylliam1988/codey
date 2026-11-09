"""Browser tool contracts and prompt presentation confined to live experiments."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

from codey.knowledge.model import canonical_json
from codey.operations.task_session import TaskSession
from codey.research.answer_context import _render_context as production_render_context
from codey.research.answer_contract import AnswerScope
from codey.research.knowledge_schema import object_schema
from codey.research.ledger import ResearchLedger
from codey.research.tool_contract import ToolContract

BROWSER_CONTRACTS = {
    "browser_snapshot": ToolContract(
        name="browser_snapshot", input_schema=object_schema({
            "max_elements": {"type": "integer", "minimum": 1, "maximum": 80},
            "max_text": {"type": "integer", "minimum": 500, "maximum": 12000},
        }),
        example='{"tool":"browser_snapshot","args":{}}',
        description="observe the retained source page as bounded text with semantic element refs; take a fresh snapshot after navigation or actions",
    ),
    "browser_read": ToolContract(
        name="browser_read", input_schema=object_schema({
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 500, "maximum": 12000},
        }),
        example='{"tool":"browser_read","args":{}}',
        description="capture and read the currently bound browser page without navigating; use after revealing content to obtain citable source passages",
    ),
    "browser_click": ToolContract(
        name="browser_click", input_schema=object_schema({"ref": {"type": "string"}}, "ref"),
        example='{"tool":"browser_click","args":{"ref":"<ref from latest browser_snapshot>"}}',
        description="click an element ref from the latest browser_snapshot once, then observe the resulting page state",
    ),
    "browser_fill": ToolContract(
        name="browser_fill", input_schema=object_schema({"ref": {"type": "string"}, "value": {"type": "string"}}, "ref", "value"),
        example='{"tool":"browser_fill","args":{"ref":"<ref from latest browser_snapshot>","value":"福州小吃"}}',
        description="fill a textbox ref from the latest browser_snapshot and verify its value; stale refs are rejected",
    ),
    "browser_scroll": ToolContract(
        name="browser_scroll", input_schema=object_schema({
            "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
            "amount": {"type": "integer", "minimum": 1, "maximum": 20000},
        }, "direction", "amount"),
        example='{"tool":"browser_scroll","args":{"direction":"down","amount":700}}',
        description="scroll the retained page by a bounded amount; take a fresh browser_snapshot afterward",
    ),
    "browser_wait": ToolContract(
        name="browser_wait", input_schema=object_schema({
            "condition": {"type": "string", "enum": ["text", "selector", "url_contains"]},
            "value": {"type": "string"},
            "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 30000},
        }, "condition", "value", "timeout_ms"),
        example='{"tool":"browser_wait","args":{"condition":"text","value":"全文","timeout_ms":5000}}',
        description="wait for one explicit page condition with a bounded timeout; timeout is a truthful failure",
    ),
}


def render_experiment_context(session: TaskSession, ledger: ResearchLedger | None, scope: AnswerScope,
                              visible: tuple[tuple[str, str], ...]) -> str:
    """Present experimental action receipts without changing production prompts."""
    text = production_render_context(session, ledger, scope, visible)
    history = [
        {"result_ref": identity, "tool": result.call.name, "args": result.call.args, "ok": result.ok,
         "observation": {key: value for key, value in observation.items() if key not in {"text", "elements"}}}
        for identity, result in session._memory_results.items()
        if isinstance((observation := result.canonical.get("browser_observation")), dict)
    ][-8:]
    if not history:
        return text
    heading = "Use the exact [eN] labels shown"
    history_text = ("Browser action history (settled receipts, not source evidence; old element refs are invalid):\n"
                    + canonical_json(history))
    return text.replace(heading, history_text + "\n\n" + heading, 1)


@contextmanager
def browser_action_context() -> Iterator[None]:
    with patch("codey.research.answer_context._render_context", render_experiment_context):
        yield
