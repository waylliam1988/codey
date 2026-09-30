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

from types import SimpleNamespace
from typing import Any

from codey.operations.research_iteration import run_research_iteration


def render_research_repair_prompt(codec: Any, plan: Any, state: Any | None = None) -> str:
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
        provider: Any,
        search: Any,
        store: Any,
        *,
        max_turns: int = 8,
        codec: Any | None = None,
        session_id: str = "",
        project: str = "",
        chat_handoff: str = "",
        trace_recorder: Any = None,
        run_id: str = "",
        tools: Any | None = None,
        iteration_context: str = "",
        topic_continuity_context: str = "",
        topic_continuity_payload: Any | None = None,
        managed_outputs: Any = None,
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
        self._last_result: Any | None = None
        if tools is not None:
            self.tools = tools
        else:
            self.tools = self._build_tools()
        self.changes = getattr(self.tools, "changes", None)

    def _build_tools(self) -> Any | None:
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
        return str(self.provider.send(message))

    def run(self, question: str) -> Any:
        from codey.operations.provider_session import ProviderAdapter

        outer = self

        class ProbeProvider(ProviderAdapter):
            def send(self, text: str, **kwargs: Any) -> str:
                return outer._send_provider(text)

        intro = self._intro(question)
        extension = intro.removeprefix(str(question)).strip()
        deps = SimpleNamespace(
            knowledge_store=self.store,
            managed_outputs=self.managed_outputs,
            runtime_mutations=None,
        )
        tools = self.tools or self._build_tools()
        iteration = run_research_iteration(
            deps,
            provider=ProbeProvider(self.provider),
            session_id=self.session_id or "research-session",
            project=self.project,
            task=str(question or ""),
            max_turns=self.max_turns,
            on_event=lambda _event: None,
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
        )
        self.tools = iteration.tools or tools
        self.changes = getattr(self.tools, "changes", None)
        self._last_result = iteration.result
        self.result = iteration.result
        yield iteration.result

    @property
    def result(self) -> Any:
        return self._last_result

    @result.setter
    def result(self, value: Any) -> None:
        self._last_result = value


__all__ = [
    "ResearchIteration",
    "render_research_repair_prompt",
]
