"""Test fixture for research iteration tests and manual benchmark probes.

Production code enters research through ``run_research_iteration``. This
module keeps only fixture concerns: input/output shapes, fake providers,
tool fixtures, and result capture. Tool behavior tests call the real
tool adapters (``ResearchTools``) or the shared kernel (``execute_turn``)
directly; protocol/state tests use ``normalize_turn`` with a real snapshot.
It never implements dispatch, validation, status shaping, or repair prompts
for production behavior (repair-prompt TEXT helper below stays for golden
protocol-text tests only).
"""

from __future__ import annotations

from collections.abc import Generator, Mapping
from unittest import mock

from codey.knowledge.store import KnowledgeStore
from codey.operations.provider_session import ProviderAdapter
from codey.operations.research_flow import ResearchFlowDeps
from codey.operations.research_iteration import run_research_iteration
from codey.operations.task_state import TaskState
from codey.providers.requests import ProviderRequest
from codey.research.run_result import ResearchRunResult
from codey.research.source_gateway import _SearchProvider
from codey.research.tools import ResearchTools
from codey.runs.trace import RunTraceRecorder
from codey.runtime.observe.events import RunEvent
from codey.storage.managed_outputs import ManagedOutputStore
from tests.support.kernel_fixture import runtime


def render_research_repair_prompt(codec: object, plan: object, state: object | None = None) -> str:
    del codec, state
    try:
        kind = str(getattr(plan, "protocol_error_kind", "") or "")
        error = str(getattr(plan, "protocol_error", "") or "invalid Research tool call")
    except Exception:
        kind, error = "", "invalid Research tool call"
    lines = [
        "Your last reply did not satisfy the Research tool contract.",
        f"Error: {error}",
        "",
    ]
    if kind == "too_many_tools":
        lines.extend([
            "Research executes exactly one action per turn.",
            "Choose one next action from the current allowed-actions block and reply with only that JSON object.",
            "Do not wrap JSON in a markdown code fence. Do not repeat the same JSON object twice.",
        ])
    elif kind == "invalid_args":
        lines.extend([
            "The tool name was recognized, but its arguments did not match the required schema.",
            "Fix the missing or invalid argument and reply with exactly one JSON object.",
        ])
    else:
        lines.extend([
            'Reply with exactly one JSON object using {"tool":"...","args":{...}} and no other text.',
        ])
    return "\n".join(lines)


class ResearchIteration:
    """Test-only thin adapter: input/output shape only, behavior via production."""

    def __init__(
        self,
        provider: object,
        search: _SearchProvider,
        store: KnowledgeStore,
        *,
        max_turns: int = 8,
        codec: object | None = None,
        session_id: str = "",
        project: str = "",
        chat_handoff: str = "",
        trace_recorder: RunTraceRecorder | None = None,
        run_id: str = "",
        tools: ResearchTools | None = None,
        iteration_context: str = "",
        topic_continuity_context: str = "",
        topic_continuity_payload: Mapping[str, object] | None = None,
        managed_outputs: ManagedOutputStore | None = None,
        controller_denied: tuple[str, ...] = (),
    ) -> None:
        # 薄适配：形状兼容旧调用（含 codec 探针字段），行为一律走生产
        # ToolSpec 校验 + ResearchTools 执行，不维护第二份转换/重试/修复提示。
        self.provider = provider
        self.search = search
        self.store = store
        self.max_turns = max(1, int(max_turns or 8))
        self.codec = codec
        self.session_id = str(session_id or "")
        self.project = str(project or "")
        self.chat_handoff = str(chat_handoff or "")
        self.trace_recorder = trace_recorder
        self.run_id = str(run_id or "research-run")
        self.iteration_context = str(iteration_context or "")
        self.topic_continuity_context = str(topic_continuity_context or "")
        self.topic_continuity_payload = topic_continuity_payload
        self.managed_outputs = managed_outputs
        self.controller_denied = controller_denied
        self._last_result: ResearchRunResult | None = None
        self.events: list[RunEvent] = []
        if tools is not None:
            self.tools = tools
        else:
            self.tools = self._build_tools()
        self.changes = self.tools.changes

    def _build_tools(self) -> ResearchTools:
        from codey.knowledge.changes import KnowledgeChanges
        from codey.research.tools import ResearchTools

        return ResearchTools(
            search=self.search, store=self.store,
            changes=KnowledgeChanges(root=self.store.root),
            session_id=self.session_id, project=self.project,
        )

    def _intro(self, question: str) -> str:
        return str(question or "")

    def _send_provider(self, message: str) -> str:
        reply = ProviderAdapter(self.provider).send(message)
        if not isinstance(reply, str):
            raise TypeError("text research fixture requires a text reply")
        return reply

    def run(self, question: str) -> Generator[ResearchRunResult, None, None]:
        from codey.operations.provider_session import ProviderAdapter

        outer = self

        class ProbeProvider(ProviderAdapter):
            def execute_request(self, request: ProviderRequest) -> object:
                from codey.providers.requests import SendTextRequest

                if isinstance(request, SendTextRequest):
                    return outer._send_provider(request.prompt)
                return super().execute_request(request)

        intro = self._intro(question)
        extension = intro.removeprefix(str(question)).strip()
        tools = self.tools
        with runtime(session_id=self.session_id or "research-session", run_id=self.run_id,
                     task=str(question or ""), provider_id="local", max_turns=self.max_turns,
                     task_kind="research") as sink:
            state = mock.Mock(spec=TaskState, durable=sink.durable, memory=None)
            deps = ResearchFlowDeps(state=state, knowledge_store=self.store, evidence_ledgers=None,
                search_factory=lambda: self.search, managed_outputs=sink.managed_outputs,
                runtime_mutations=sink.mutations)
            iteration = run_research_iteration(
                deps,
                provider=ProbeProvider(self.provider),
                session_id=self.session_id or "research-session",
                project=self.project,
                task=str(question or ""),
                max_turns=self.max_turns,
                on_event=self.events.append,
                stop_flag=None,
                provider_id="local",
                run_id=self.run_id,
                chat_handoff=self.chat_handoff,
                trace_recorder=self.trace_recorder,
                search=self.search,
                tools=tools,
                iteration_context="\n\n".join(x for x in (self.iteration_context, extension) if x),
                topic_continuity_context=self.topic_continuity_context,
                topic_continuity_payload=self.topic_continuity_payload,
                controller_denied=self.controller_denied,
            )
        self.tools = iteration.tools or tools
        self.changes = self.tools.changes
        self._last_result = iteration.result
        self.result = iteration.result
        yield iteration.result

    @property
    def result(self) -> ResearchRunResult | None:
        return self._last_result

    @result.setter
    def result(self, value: ResearchRunResult | None) -> None:
        self._last_result = value


__all__ = [
    "ResearchIteration",
    "render_research_repair_prompt",
]
