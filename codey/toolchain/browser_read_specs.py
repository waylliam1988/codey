"""Tool definitions for browser reads and observed detail disclosures."""
from __future__ import annotations

from codey.toolchain.constants import PAGE_DETAIL_TOOL
from codey.toolchain.tool_spec import ToolSpec


def browser_read_specs() -> dict[str, ToolSpec]:
    return {
        "browser_read": ToolSpec(
            name="browser_read", grant="browser.observe", executor="browser",
            namespace="browser.read", effect="read", evidence_route="source_read",
            parameters=(("offset", {"type": "integer", "minimum": 0}),
                        ("limit", {"type": "integer", "minimum": 500, "maximum": 12000})),
            description="Capture the currently bound page through the Research source and passage pipeline.",
            json_examples=('{"tool":"browser_read","args":{}}',),
            preconditions=("browser_bound",),
        ),
        PAGE_DETAIL_TOOL: ToolSpec(
            name=PAGE_DETAIL_TOOL, grant="browser.interact", executor="browser",
            namespace="browser.read", effect="read", evidence_route="source_read",
            parameters=(("choice", {"type": "string"}),), required=("choice",),
            description="Read an observed, native detail disclosure within the requested record.",
            preconditions=("browser_bound", "observed_disclosure"),
        ),
    }
