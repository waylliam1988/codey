"""Shared toolchain constants owned below the definition/runtime layers.

MAX_REPLACEMENTS is the single edit-fanout bound used both by the tool
contract description (definition) and the executor (runtime). It lives here
so definition.py never imports runtime.py for one integer.
"""

from __future__ import annotations

MAX_REPLACEMENTS = 8
SEARCH_PAGE_MAX_RESULTS = 100
EXACT_REPLACEMENT_CONTEXT_HINT = (
    "Include surrounding lines and the target text together in old_string of the same replacement "
    "so it matches one location uniquely. Preserve those surrounding lines in new_string. "
    "Separate replacement objects do not scope one another."
)
