"""Production tool execution for the unified kernel (operations layer).

Project tools reuse the coding guards and readers (path safety, command
policy, shell approval surface); Research tools reuse ResearchTools, the
source gateway, and the evidence ledger. Completion facts come only from
successful tool results and ledger
state, never from substring matching or model-supplied parameters.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from codey.operations.browser.browser_detail import read_browser_source
from codey.operations.browser.execution import BrowserExecutor
from codey.operations.project.execution import ProjectExecutor
from codey.operations.research.execution import ResearchExecutor
from codey.operations.research_resources import (
    bind_browser_session as bind_browser_session,
)
from codey.operations.research_resources import (
    build_research_tools as build_research_tools,
)
from codey.operations.research_resources import (
    close_research_tools as close_research_tools,
)
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import (
    JsonObject,
)

if TYPE_CHECKING:
    from codey.agents.request import ChangeTracker
    from codey.agents.tools import AgentToolFns
    from codey.automation.browser_session import BrowserSession
    from codey.durable.session import DurableSession
    from codey.operations.kernel_contracts import ToolExecutor
    from codey.operations.task_session import TaskSession
    from codey.policies.action import ActionPolicyDecision
    from codey.research.answer_contract import AnswerScope
    from codey.storage.managed_outputs import ManagedOutputStore
    from codey.toolchain.tool_spec import ToolSpec
from codey.research.tools import ResearchTools


def effective_project_profile(permission_profile: object) -> str:
    """Project-guard profile shared by execution and shell approval.

    The Research profile controls research context and source tools. A
    project tool still needs the coding path/command guard; TaskPolicy
    has already authorized the individual project capability. Approval
    must evaluate with exactly this profile, never a hardcoded duplicate.
    """
    return "coding_writer" if str(permission_profile or "") == "research" else str(
        permission_profile or "coding_writer")


@dataclass(frozen=True)
class ProjectPolicyCheck:
    denied: bool
    message: str = ""
    approval_required: bool = False
    reason_code: str = ""


class ExecutionDelegate:
    """Real guards + real tools behind the kernel's policy snapshot."""

    def __init__(
        self,
        *,
        session: TaskSession | None = None,
        project_path: Path | str | None = None,
        tool_fns: AgentToolFns | None = None,
        research_tools: ResearchTools | None = None,
        browser_session: BrowserSession | None = None,
        browser_wait_document: JsonObject | None = None,
        permission_profile: str = "coding_writer",
        approval_available: bool = False,
        change_tracker: ChangeTracker | None = None,
        managed_outputs: ManagedOutputStore | None = None,
        session_id: str = "",
        run_id: str = "",
        frozen_specs: Mapping[str, ToolSpec] | None = None,
        custom_executors: Mapping[str, ToolExecutor | None] | None = None,
        research_scope: AnswerScope | None = None,
        durable: DurableSession | None = None,
    ) -> None:
        self.session = session
        self.project_path: Path | None = Path(str(project_path)).expanduser() if project_path else None
        if self.project_path is not None and not self.project_path.is_dir():
            self.project_path = None
        self.tool_fns: AgentToolFns | None = tool_fns
        self.research_tools = research_tools
        self.browser_session = browser_session or (research_tools.browser_session if research_tools is not None else None)
        self.permission_profile = (permission_profile or "coding_writer")
        self.approval_available = approval_available
        self.change_tracker = change_tracker
        self.managed_outputs = managed_outputs
        self.session_id = session_id
        self.run_id = run_id
        # 本轮冻结绑定：非 None 时解析/执行不再查询实时注册表。
        self._frozen_specs = frozen_specs
        self._custom_executors = custom_executors
        self._research_scope = research_scope
        self.durable = durable
        self.browser_executor = BrowserExecutor(session=session, browser_session=self.browser_session,
            wait_document=browser_wait_document, permission_profile=self.permission_profile,
            managed_outputs=managed_outputs, session_id=session_id, run_id=run_id,
            trace_research=research_scope is not None, read_source=self.read_browser_source)
        if self.tool_fns is None and self.project_path is not None:
            from codey.agents.tools import build_project_tool_fns

            self.tool_fns = build_project_tool_fns(
                state_home=managed_outputs.root.parent if managed_outputs is not None else None,
            )

        self.project_executor = ProjectExecutor(session=session, project_path=self.project_path,
            tool_fns=self.tool_fns, browser_session=self.browser_session, change_tracker=change_tracker,
            permission_profile=self._project_permission_profile(), check_policy=self.check_policy)
        self.research_executor = ResearchExecutor(session=session, research_tools=research_tools,
            browser_session=self.browser_session, research_scope=research_scope, managed_outputs=managed_outputs,
            session_id=session_id, run_id=run_id, permission_profile=self.permission_profile)

    def read_browser_source(self, browser: BrowserSession, call: ToolCall, *, turn: int,
                            tool_index: int) -> tuple[ToolResult, bool, int | None]:
        return read_browser_source(self.research_executor, browser, call,
            spec=self._resolve_spec(call.name) if self._frozen_specs is not None else None,
            turn=turn, tool_index=tool_index)

    def _resolve_spec(self, name: str) -> ToolSpec | None:
        lowered = (name or "").strip().lower()
        if self._frozen_specs is not None:
            return self._frozen_specs.get(lowered)
        try:
            from codey.toolchain.tool_spec import spec_for_tool
        except Exception:
            return None
        try:
            return spec_for_tool(lowered)
        except Exception:
            return None

    def _resolve_custom_executor(self, name: str) -> Callable[[ToolCall], object] | None:
        lowered = (name or "").strip().lower()
        if self._custom_executors is not None:
            return self._custom_executors.get(lowered)
        try:
            from codey.toolchain.tool_spec import custom_executor_for
        except Exception:
            return None
        try:
            return custom_executor_for(lowered)
        except Exception:
            return None

    def handles(self, name: str) -> bool:
        spec = self._resolve_spec(name)
        if spec is None:
            return False
        if spec.executor == "receipt":
            return bool(self.session is not None)
        if spec.executor == "project":
            return self.project_path is not None and self.tool_fns is not None
        if spec.executor in {"source", "knowledge"}:
            return bool(self.research_tools is not None)
        if spec.executor == "browser":
            # Route unavailable resources to a deterministic production error;
            # never fall through to a custom executor for a built-in tool.
            return True
        try:
            if self._resolve_custom_executor(name or "") is not None:
                return True
        except Exception:
            pass
        return False

    def execute(self, call: ToolCall, *, turn: int = 0,
                tool_index: int = 0) -> tuple[ToolResult, bool, int | None]:
        name = (call.name or "").strip().lower()
        try:
            spec = self._resolve_spec(name)
            executor = spec.executor if spec is not None else ""
        except Exception:
            executor = ""
        if executor == "receipt":
            if name in {"read_data", "calculate"}:
                result = self.research_executor.calculate(call)
                return result, result.ok, None
            from codey.operations.tool_result_reader import ReceiptReader, read_tool_result

            assert self.session is not None
            result = read_tool_result(ReceiptReader(self.session, self.managed_outputs, self.session_id, self.run_id),
                                      call, durable=self.durable)
            return result, result.ok, None
        if executor == "project":
            return self.project_executor.execute(call)
        if executor == "browser":
            return self._execute_browser(call, turn=turn, tool_index=tool_index)
        if executor in {"source", "knowledge"}:
            return self.research_executor.execute(call, turn=turn, tool_index=tool_index)
        # Third-task tools run via the frozen turn binding when present,
        # otherwise the live registry.
        try:
            fn = self._resolve_custom_executor(name)
        except Exception:
            fn = None
        if callable(fn):
            try:
                produced = fn(call)
            except Exception as exc:
                result = ToolResult(ok=False, call=call, model_text=f"ERROR: {exc or 'tool failed'}")
                return result, False, None
            if not isinstance(produced, ToolResult):
                result = ToolResult(ok=False, call=call, model_text="ERROR: executor must return a structured ToolResult")
                return result, False, None
            return produced, produced.ok, None
        result = ToolResult(ok=False, call=call, model_text=f"ERROR: no production executor for {name or '?'}")
        return result, False, None

    def _execute_browser(self, call: ToolCall, *, turn: int,
                         tool_index: int) -> tuple[ToolResult, bool, int | None]:
        check = self.check_policy(call)
        if check.denied:
            from codey.operations.kernel_result import policy_denial_result

            return policy_denial_result(call, check.message, reason_code=check.reason_code), False, None
        return self.browser_executor.execute(call, turn=turn, tool_index=tool_index)

    def check_policy(self, call: ToolCall) -> ProjectPolicyCheck:
        """Keep machine-readable denial separate from its explanation."""

        spec = self._resolve_spec(call.name)
        if spec is None:
            return ProjectPolicyCheck(True, "tool definition unavailable", False)
        if self.session is None:
            return ProjectPolicyCheck(True, "task session unavailable", False)
        if not self.session.policy.allows(spec.grant):
            return ProjectPolicyCheck(True, f"task policy denied {call.name}", False)
        if spec.executor != "project":
            return ProjectPolicyCheck(False, "", False)
        if self.project_path is None:
            return ProjectPolicyCheck(True, "no associated project", False)
        try:
            from codey.agents.tool_execution import (
                evaluate_tool_call_policy_for,
                policy_asks_user,
                policy_denied,
            )
        except Exception as exc:
            return ProjectPolicyCheck(True, f"policy unavailable: {exc}", False)
        try:
            runtime_name = {"list_dir": "ls", "read_file": "read", "grep": "search",
                            "find_references": "references", "semantic_definition": "read",
                            "workspace_diff": "ls", "syntax_query": "read"}.get(call.name, call.name)
            evaluated: tuple[ActionPolicyDecision | None, object] = evaluate_tool_call_policy_for(
                ToolCall(runtime_name, dict(call.args or {}), call_id=call.call_id),
                project=self.project_path,
                permission_profile=self._project_permission_profile(),
                approval_available=self.approval_available,
                phase="writer",
                shell=self.session.shell,
            )
            decision = evaluated[0]
        except Exception as exc:
            return ProjectPolicyCheck(True, f"policy error: {exc}", False)
        if decision is not None and policy_denied(decision):
            reason = decision.reason_code or "denied"
            display = decision.display
            if reason == "command_not_allowed" and not self.session.policy.allows("shell.approval"):
                display = ("This task mode cannot request shell approval. Use an interactive coding task "
                           "with command approvals enabled, or use an allowed verification command.")
            elif reason == "command_not_allowed":
                display += " Submit this command on its own in an interactive task to request approval."
            return ProjectPolicyCheck(True, f"project guard denied {call.name}: {reason}; {display}", False, reason)
        try:
            if (call.name or "") == "shell" and policy_asks_user(decision):
                return ProjectPolicyCheck(True, "shell requires user approval", True)
        except Exception:
            pass
        return ProjectPolicyCheck(False, "", False)


    def _project_permission_profile(self) -> str:
        return effective_project_profile(self.permission_profile)

__all__ = ["ExecutionDelegate", "build_research_tools", "close_research_tools"]
