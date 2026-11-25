"""Resolve the project inputs authorized by a frozen model turn."""
from __future__ import annotations

from typing import TYPE_CHECKING

from codey.codemode.verification import resolve_verification
from codey.operations.project.declared_field_input import lower
from codey.operations.project.edit_base import (
    bind_symbol_read,
    bound_symbol_retry,
    lower_compact_symbol_edit,
    raw_symbol_content,
)
from codey.protocols.text_turn import DecodedTextTurn, TextAnswer
from codey.providers.base import AssistantTurn, TurnFinish
from codey.types.json_value import ToolArgs, canonical_json_object

if TYPE_CHECKING:
    from codey.operations.kernel.turn_contract import TurnSnapshot
    from codey.toolchain.spec_model import ToolSpec


def lower_project_input(
    args: ToolArgs, model: ToolSpec, specs: tuple[ToolSpec, ...], symbol_reads: tuple[tuple[str, str], ...],
) -> ToolArgs:
    if model.name == "run":
        spec = next(spec for spec in specs if spec.name == "run")
        return resolve_verification(args, {"properties": canonical_json_object(dict(spec.parameters))})
    return bind_symbol_read(lower_compact_symbol_edit(args, model), dict(symbol_reads))


def lower_declared_fields(args: ToolArgs, specs: tuple[ToolSpec, ...]) -> ToolArgs:
    return lower(args, next(spec for spec in specs if spec.name == "edit"))


def bound_symbol_source(reply: DecodedTextTurn | AssistantTurn, snapshot: TurnSnapshot) -> str:
    if bound_symbol_retry(snapshot) is None:
        return ""
    if (isinstance(reply, AssistantTurn) and reply.raw.get("text_protocol") is True
            and reply.finish is TurnFinish.COMPLETE and not reply.tool_calls):
        return raw_symbol_content(reply.text)
    return raw_symbol_content(reply.text) if isinstance(reply, TextAnswer) else ""
