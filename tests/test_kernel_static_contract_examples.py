"""Static negative examples for the kernel's typed contracts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

BAD_CONTRACT = '''
from codey.operations.kernel_contracts import DeliveryStatus, ToolExecutor
from codey.runtime.core.models import ToolCall


def wrong_executor(call: ToolCall) -> str:
    return "bad result"


executor: ToolExecutor = wrong_executor
delivery: DeliveryStatus = "not-a-delivery-status"
'''

BAD_PROVIDER_CALL = '''
from collections.abc import Sequence
from codey.operations.kernel_protocol import _native_tool_objects


calls: Sequence[str] = ["not-a-provider-call"]
_native_tool_objects(calls)
'''

BAD_JSON_ARGUMENTS = '''
from codey.runtime.core.models import ToolCall
from codey.providers.base import ProviderToolCall

ToolCall(name="read", args={"path": object()})
ProviderToolCall(id="call", name="read", arguments={"path": object()})
'''

BAD_TEXT_SPAN = '''
from codey.providers.text_result_views import TextResultSpan

TextResultSpan(start="0", end=2, digest="hash", result_ref="result", tool="read", ok=True)
'''

BAD_PROVIDER_REQUEST = '''
from codey.providers.requests import SendTextRequest, SendToolResultsBoundRequest

SendTextRequest(prompt=["not-text"])
SendToolResultsBoundRequest(results=(), window_ref=None, tools=())
'''

BAD_PROVIDER_WRAPPER = '''
from codey.operations.provider_session import ObservedProvider, ProviderAdapter

def old_observer(method: str, args: tuple[object, ...], kwargs: dict[str, object]) -> None:
    pass

ObservedProvider(object(), old_observer)
ProviderAdapter(object()).send(object())
ProviderAdapter(object()).missing_extension()

from codey.operations.kernel_contracts import KernelTransportDeps, KernelExecutionDeps
from codey.operations.provider_request import prepare_provider_request

KernelTransportDeps(provider=object())
KernelExecutionDeps(research_tools=object())
prepare_provider_request(object(), "send", ("prompt",), {}, counters=None)
'''

BAD_COMPLETION_INPUT = '''
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_inputs import CompletionCheckInput
from codey.operations.completion_gate import register_completion_check_provider
from codey.operations.task_session import TaskSession

CompletionContext(selected_check="pytest")
CompletionContext(changes={"files": []})

def mutable_session_check(session: TaskSession) -> list:
    session.edited_files.clear()
    return []

register_completion_check_provider("mutable", mutable_session_check)

def mutate(inputs: CompletionCheckInput) -> None:
    inputs.edited_files["injected.py"] = 9
'''

BAD_RESEARCH_PAYLOAD = '''
from codey.research.answer_values import AnswerSubmission
from codey.research.tool_args import KnowledgeCommitRequest
from codey.research.tools import ResearchToolOutput

submission: AnswerSubmission = {
    "schema_version": 1, "task_digest": "digest", "markdown": "answer", "state": "unresolved",
    "scope": {"task_digest": "digest", "aliases": [], "attempt_refs": [], "schema_version": 4},
    "citations": [],
}  # A completed submission must carry its digest.
KnowledgeCommitRequest(commit_id="commit", mutations=("not-a-mutation",))
ResearchToolOutput("bad", read_passages=({"ref": False, "start": 0, "end": 1},), ok=True)
'''


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("mypy", ("--python-version", "3.11")),
        ("pyrefly", ("check",)),
        ("ty", ("check",)),
    ],
)
def test_type_checker_rejects_invalid_kernel_contract(tool: str, arguments: tuple[str, ...]) -> None:
    source = ROOT / "tests" / f"_bad_kernel_contract_{tool}.py"
    source.write_text(BAD_CONTRACT, encoding="utf-8")
    try:
        result = subprocess.run(
            [sys.executable, "-m", tool, *arguments, str(source)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        output = (result.stdout or "") + (result.stderr or "")
    finally:
        source.unlink(missing_ok=True)
    assert result.returncode != 0, output
    assert f"_bad_kernel_contract_{tool}" in output, output
    assert any(marker in output for marker in ("Incompatible", "not assignable", "assignment", "does not match")), output


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("mypy", ("--python-version", "3.11")),
        ("pyrefly", ("check",)),
        ("ty", ("check",)),
    ],
)
@pytest.mark.parametrize("source_text", [BAD_PROVIDER_CALL, BAD_JSON_ARGUMENTS, BAD_TEXT_SPAN, BAD_PROVIDER_REQUEST,
                                       BAD_PROVIDER_WRAPPER, BAD_COMPLETION_INPUT, BAD_RESEARCH_PAYLOAD],
                         ids=["provider-call", "json-args", "text-span", "provider-request", "provider-wrapper",
                              "completion-input", "research-payload"])
def test_type_checker_rejects_untyped_provider_call_shape(
    tool: str, arguments: tuple[str, ...], source_text: str,
) -> None:
    source = ROOT / "tests" / f"_bad_kernel_provider_call_{tool}.py"
    source.write_text(source_text, encoding="utf-8")
    try:
        result = subprocess.run(
            [sys.executable, "-m", tool, *arguments, str(source)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        output = (result.stdout or "") + (result.stderr or "")
    finally:
        source.unlink(missing_ok=True)
    assert result.returncode != 0, output
    assert f"_bad_kernel_provider_call_{tool}" in output, output
    assert any(marker in output for marker in (
        "Incompatible", "incompatible type", "not assignable", "argument", "Sequence",
    )), output
