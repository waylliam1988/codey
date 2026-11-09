"""Read-source calls select captured versions before the common result renderer."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from codey.research.ledger import ResearchLedger
from codey.research.source_snapshots import captured_source
from codey.research.tools import ResearchToolOutput, ResearchTools
from codey.runtime.core.execution_record import HitTarget
from codey.runtime.core.models import ToolCall
from codey.toolchain.tool_spec import _CONTROLLER_ALIAS_ID_ARG
from codey.types.json_value import ToolArgs, require_json_integer, require_json_string


class SourceReferenceView(Protocol):
    @property
    def search_results(self) -> Mapping[str, str]: ...

    @property
    def source_ids(self) -> Mapping[str, str]: ...

    @property
    def hit_targets(self) -> Mapping[str, HitTarget]: ...


def resolve_alias_url(session: SourceReferenceView | None, alias: str, args: ToolArgs) -> str:
    definition = _CONTROLLER_ALIAS_ID_ARG.get(alias)
    rid = str(args.get(definition[1]) or "").strip().lower() if definition is not None else ""
    if not rid or session is None:
        return ""
    if alias == "open_hit":
        target = session.hit_targets.get(rid)
        return target["url"] if target is not None else ""
    return session.search_results.get(rid, "") or session.source_ids.get(rid, "")


def open_source(call: ToolCall, tools: ResearchTools, ledger: ResearchLedger, url: str,
                target: HitTarget | None = None) -> ResearchToolOutput:
    offset = require_json_integer(call.args.get("offset", 0), "offset")
    limit = require_json_integer(call.args.get("limit", 6000), "limit")
    pages = require_json_string(call.args.get("pages", ""), "pages")
    if call.name == "open_url" and (not offset or not ledger.canonical_opened_url(url)):
        return tools.open_url(url, offset=offset, limit=limit, pages=pages)
    if call.name == "open_result":
        return tools.open_url(url)
    if target is not None:
        offset, pages = target["offset"], target["pages"]
        if pages:
            # PDF search offsets are relative to the page body. The captured
            # single-page document also contains the generated page label.
            offset += len(f"[page {pages}]\n")
    observation = captured_source(ledger, url, target["text_hash"] if target is not None else "")
    return tools.read_source(observation, offset=offset, limit=limit, pages=pages)
