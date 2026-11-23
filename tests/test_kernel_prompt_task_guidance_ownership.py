"""Task guidance belongs to task composition, not to the shared prompt renderer."""

import ast
import hashlib
import inspect
from contextlib import closing
from itertools import product
from pathlib import Path

import pytest

from codey.durable import DurableSession, MemoryDurableStore
from codey.operations import kernel_prompt
from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_loop import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
    run_task_kernel,
)
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, tools_from_specs
from codey.providers.requests import ProviderRequest, SendTextRequest, SendTurnRequest
from codey.providers.transport import ProviderTransport
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import decode_json_object, parse_json_object


def _guidance(policy: TaskPolicy) -> str:
    from codey.operations.task_guidance import task_guidance_for_policy

    return task_guidance_for_policy(policy, task_kind="project")


def test_shared_strict_guidance_reviewed_prompt_digest() -> None:
    policy = TaskPolicy(grants=frozenset({"control", "web.read", "knowledge.read", "knowledge.write"}),
                        strict_research=True)
    assert hashlib.sha256(_guidance(policy).encode()).hexdigest() == (
        # Reviewed: adopt exact saved findings without final rewriting; attempts come from receipts.
        "861f1d5c44a5bbcdcda2a11105fb775422155acaecee3d2a12023f1ecd185034"
    )


@pytest.mark.parametrize("kind", ["research", "project", "chat", "evidence_followup"])
def test_task_guidance_selects_completion_rules_without_losing_output_requirements(kind: str) -> None:
    from codey.operations.task_guidance import task_guidance_for_policy
    from codey.policies.output_requirement import JSON_ANSWER_GUIDANCE

    policy = TaskPolicy(grants=frozenset({"control", "web.read", "knowledge.read", "knowledge.write"}),
                        strict_research=True, required_checks=("output_json",))
    text = task_guidance_for_policy(policy, task_kind=kind)
    assert JSON_ANSWER_GUIDANCE in text
    for legacy in ("Register 1..8 questions with research_plan", "Submit only report_sections.coverage"):
        assert (legacy in text) is (kind != "research")
    if kind == "research":
        assert "save_finding" not in text
        assert "knowledge_commit" not in text


@pytest.mark.parametrize("native", [False, True])
def test_shared_kernel_sends_arbitrary_task_guidance_in_both_protocols(
    native: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from codey.env_names import NATIVE_TOOLS_ENV

    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1" if native else "0")
    sent: list[str] = []

    class Provider(ProviderTransport):
        provider = object()

        def execute_request(self, request: ProviderRequest) -> str | AssistantTurn:
            assert isinstance(request, SendTextRequest | SendTurnRequest), "no tool calls were issued"
            sent.append(request.prompt)
            return AssistantTurn(text="not a tool call") if isinstance(request, SendTurnRequest) else "not a tool call"

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})),
                          task_kind="third_task", max_turns=1)
    with closing(DurableSession(MemoryDurableStore())) as durable:
        durable.admit_input(conversation_id="s", request_id="input", task_id="r", text="test task", provider_snapshot={})
        durable.claim_task("r")
        durable.start_task("r", state={})
        mutations = RuntimeMutationLine(durable=durable)
        mutations.accept_operation(session_id="s", run_id="r", provider_id="local", turn_budget=1,
                                   max_repair_rounds=1, task_kind=session.task_kind)
        mutations.mark_writer_running("s", "r", provider_id="local")
        sink = KernelEffectSink(mutations, session_id="s", run_id="r", provider_id="local",
                               durable=durable, managed_outputs=ManagedOutputStore(tmp_path))
        run_task_kernel(session, request=KernelRunRequest(
            transport=KernelTransportDeps(provider=Provider(), provider_id="local", run_id="r"),
            execution=KernelExecutionDeps(intent_sink=sink),
            observation=KernelObservationDeps(on_boundary=durable_generation_boundary_callback(
                durable=durable, session_id="s", task_id="r", sink=sink)),
            task_guidance="Third task completion: preserve the supplied units.",
        ))
    assert len(sent) == 1
    assert "Third task completion: preserve the supplied units." in sent[0]
    assert "Research evidence rules" not in sent[0]


def test_ordinary_web_task_has_no_strict_research_guidance() -> None:
    policy = TaskPolicy(grants=frozenset({"control", "project.read", "web.read"}))
    assert _guidance(policy) == ""


@pytest.mark.parametrize("grants, forbidden", [
    ({"control"}, ("web_search", "open_result", "open_url", "knowledge_write", "knowledge_read")),
    ({"control", "web.read"}, ("knowledge_write", "knowledge_read")),
    ({"control", "web.read", "knowledge.write"}, ("knowledge_read",)),
    ({"control", "knowledge.read"}, ("web_search", "open_result", "open_url", "knowledge_write")),
])
def test_research_guidance_does_not_direct_unauthorized_tools(grants: set[str], forbidden: tuple[str, ...]) -> None:
    text = _guidance(TaskPolicy(grants=frozenset(grants), strict_research=True))
    assert "report_sections.coverage" in text
    for name in forbidden:
        assert name not in text


def test_research_guidance_respects_explicit_capability_denial() -> None:
    policy = TaskPolicy(grants=frozenset({"control", "web.read", "knowledge.read", "knowledge.write"}),
                        denied_capabilities=frozenset({"knowledge.write"}), strict_research=True)
    assert "knowledge_write" not in _guidance(policy)


def test_all_guidance_grant_and_denial_combinations_remain_within_authorization() -> None:
    capabilities = ("web.read", "knowledge.read", "knowledge.write")
    tool_capabilities = {"web_search": "web.read", "open_result": "web.read", "open_url": "web.read",
                         "knowledge_read": "knowledge.read", "knowledge_write": "knowledge.write"}
    for flags in product((False, True), repeat=6):
        policy = TaskPolicy(
            grants=frozenset({"control", *(cap for cap, enabled in zip(capabilities, flags[:3], strict=True) if enabled)}),
            denied_capabilities=frozenset(cap for cap, denied in zip(capabilities, flags[3:], strict=True) if denied),
            strict_research=True,
        )
        text = _guidance(policy)
        for tool, capability in tool_capabilities.items():
            if tool in text:
                assert policy.allows(capability), (flags, tool)


@pytest.mark.parametrize("native", [False, True])
def test_note_id_usage_is_owned_by_the_shared_tool_contract(native: bool) -> None:
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.research.tool_contract import knowledge_read_id_guidance

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "knowledge.read"})))
    snapshot = build_turn_snapshot(session, native=native)
    if native:
        descriptions = {row.name: row.description for row in tools_from_specs(snapshot.frozen_specs)}
        assert knowledge_read_id_guidance() in descriptions["knowledge_read"]
    else:
        assert knowledge_read_id_guidance() in snapshot.contract_text


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("denied", [False, True])
def test_note_reader_description_respects_authorized_write_visibility(native: bool, denied: bool) -> None:
    from codey.operations.kernel_protocol import build_turn_snapshot

    session = TaskSession(policy=TaskPolicy(
        grants=frozenset({"control", "web.read", "knowledge.read", "knowledge.write"}), strict_research=True,
        denied_capabilities=frozenset({"knowledge.write"}) if denied else frozenset(),
    ), task_kind="research", research_questions=("What is supported?",))
    snapshot = build_turn_snapshot(session, native=native)
    assert "knowledge_read" in snapshot.tool_names
    assert ("knowledge_write" in snapshot.tool_names) is not denied
    if native:
        description = next(row.description for row in tools_from_specs(snapshot.frozen_specs)
                           if row.name == "knowledge_read")
    else:
        description = next(spec.description for spec in snapshot.model_specs if spec.name == "knowledge_read")
        assert description in snapshot.contract_text
    assert "knowledge_write" not in description
    assert "do not prepend facts/ or append .md" in description


def test_report_compilation_guidance_does_not_request_model_written_sections() -> None:
    text = _guidance(TaskPolicy(grants=frozenset({"control"}), strict_research=True))
    assert "Submit only report_sections.coverage" in text
    assert "Do not supply answer prose" in text
    assert "application compiles the report" in text


def test_kernel_prompt_has_no_domain_branch_or_completion_dependencies() -> None:
    source = inspect.getsource(kernel_prompt)
    assert "strict_research" not in source
    assert "Research evidence rules" not in source
    tree = ast.parse(source)
    forbidden = ("codey.research", "codey.operations.project_completion_checks",
                 "codey.operations.project_verification")
    domain_imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                      and (node.module or "").startswith(forbidden)]
    # Protocol repair recognizes the frozen whole-answer scope to preserve an
    # unsubmitted draft. Task guidance still belongs outside the prompt renderer.
    repair = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_repair_prompt")
    for node in domain_imports:
        assert node in list(ast.walk(repair))
        assert node.module == "codey.research.answer_contract"
        assert [(name.name, name.asname) for name in node.names] == [("AnswerScope", None)]


@pytest.mark.parametrize("native", [False, True])
def test_completion_followup_preserves_submitted_report_format(native: bool) -> None:
    text = kernel_prompt._completion_followup("Fix the unbound sentence.", native=native, structured_report=True)
    assert "Repair only the reported problem using the allowed actions" in text
    assert "Preserve the submitted question answers" not in text
    assert "Do not repeat an unchanged selection" in text
    assert '"summary":"##' not in text
    ordinary = kernel_prompt._completion_followup("Fix the requested answer.", native=native)
    assert "Preserve report_sections" not in ordinary
    assert "## 结论" not in ordinary


def test_strict_report_has_one_preferred_example_without_changing_ordinary_done() -> None:
    from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
    from codey.protocols.text_turn import decode_text_turn
    from codey.research.tool_contract import STRUCTURED_REPORT_EXAMPLE

    strict = TaskSession(policy=TaskPolicy(grants=frozenset({"control"}), strict_research=True))
    ordinary = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})))
    def examples(session: TaskSession) -> tuple[str, ...]:
        return next(spec.json_examples for spec in build_turn_snapshot(session).frozen_specs if spec.name == "done")
    assert len(examples(strict)) == 1
    args = decode_json_object(parse_json_object(examples(strict)[0])["args"])
    assert set(args) == {"report_sections"}
    sections = decode_json_object(args["report_sections"])
    assert set(sections) == {"coverage"}
    assert sections["coverage"] == [
        {"question_ref": "q1", "state": "partial", "findings": ["f1"]}]
    assert STRUCTURED_REPORT_EXAMPLE not in examples(ordinary)
    ordinary_snapshot = build_turn_snapshot(ordinary)
    ordinary_done = next(s for s in ordinary_snapshot.model_specs if s.name == "done")
    assert ordinary_done.required == ("summary",)
    assert "report_sections" not in dict(ordinary_done.parameters)
    assert not normalize_turn(decode_text_turn(ordinary_done.json_examples[0]), snapshot=ordinary_snapshot).protocol_error


def test_request_scoped_contract_prompt_does_not_offer_ordinary_prose_or_duplicate_declarations() -> None:
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})))
    text = kernel_prompt.kernel_prompt_for_session(session, tool_names=("done",),
        contract_text="FROZEN DECLARATIONS", request_scoped_contract=True)
    assert "FROZEN DECLARATIONS" not in text
    assert "Deliver the final answer with done" in text
    assert "as normal text" not in text and "Answer directly" not in text


@pytest.mark.parametrize("native", [False, True])
def test_project_task_occurs_once_in_assembled_prompt(tmp_path: Path, native: bool) -> None:
    from codey.agents.request import AgentRequest
    from codey.operations.project_adapter import _project_context
    from codey.operations.provider_session import ProviderAdapter

    task = "TASK_UNIQUE_09c4 read the project"
    workspace = _project_context(AgentRequest(project=tmp_path, task=task, provider=ProviderAdapter(object())))
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), task_text=task)
    prompt = kernel_prompt.kernel_prompt_for_session(
        session, tool_names=(), context_text=workspace, native=native,
    )
    assert task not in workspace
    assert task not in kernel_prompt.working_context(session)
    assert prompt.count(task) == 1
