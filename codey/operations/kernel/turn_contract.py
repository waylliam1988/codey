"""Frozen tool inventory, model declarations and execution bindings for a turn."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from codey.toolchain.spec_model import ToolSpec
from codey.toolchain.tool_spec import freeze_spec_parameters

if TYPE_CHECKING:
    from codey.operations.browser.browser_copy import CopyTurn
    from codey.operations.browser.browser_references import BrowserReferences
    from codey.operations.kernel_contracts import ToolExecutor
    from codey.operations.project.project_instructions import ProjectInstructionBinding
    from codey.policies.task_policy import TaskPolicy
    from codey.research.answer_contract import AnswerScope


@dataclass(frozen=True)
class ModelInputBinding:
    """The captured model input; conversion still targets the original executor."""

    kind: Literal["upload", "browser_copy", "declared_fields"]
    model_spec: ToolSpec
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_spec", freeze_spec_parameters(self.model_spec))


@dataclass(frozen=True)
class TurnSnapshot:
    """One authoritative per-round observation: policy ∩ current facts.

    Built once at round start; any build failure raises and the kernel
    terminates the turn as controller_failure (fail-closed), never a stale
    fallback list. Carries the allowed tools, frozen tool definitions plus
    both protocols' contracts for the round. The snapshot object贯穿发送、
    解析与执行：注册表变化下一轮生效，本轮解析与执行共用同一冻结定义。
    ``frozen_specs`` are nested-immutable (mapping proxies inside); the
    ``custom_executors`` table freezes third-task bindings so a mid-turn
    registry swap cannot reroute this turn's execution.
    """

    allowed: tuple[str, ...] | None
    contract_text: str
    model_specs: tuple[ToolSpec, ...]
    tool_names: tuple[str, ...] = ()
    policy: TaskPolicy | None = None
    frozen_specs: tuple[ToolSpec, ...] = ()
    custom_executors: tuple[tuple[str, ToolExecutor | None], ...] = ()
    implementation_compatible: bool = True
    nested_tool_names: tuple[str, ...] = ()
    research_scope: AnswerScope | None = None
    ignore_runtime_claims: bool = False
    browser_copy: CopyTurn | None = None
    input_binding: ModelInputBinding | None = None
    task_navigation_url: str | None = None
    task_source_url: str | None = None
    browser_followups: bool = False
    browser_fill_text: str | None = None
    browser_references: BrowserReferences | None = None
    project_instructions: tuple[ProjectInstructionBinding, ...] = ()
    navigation_verifies: bool = False
    data_file_reads: tuple[str, ...] | None = None
    symbol_read_refs: tuple[tuple[str, str], ...] = ()

    @property
    def navigation_observes(self) -> bool:
        return self.browser_followups and "browser_snapshot" in self.nested_tool_names

    @property
    def inventory_names(self) -> tuple[str, ...]:
        names: dict[str, None] = dict.fromkeys((*self.tool_names, *self.nested_tool_names))
        return tuple(names)

    @property
    def has_project_tools(self) -> bool:
        return "codemode" in self.tool_names and bool({"edit", "read_file"}.intersection(self.tool_names))
