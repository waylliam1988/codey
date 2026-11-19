"""Shared toolchain constants owned below the definition/runtime layers.

MAX_REPLACEMENTS is the single edit-fanout bound used both by the tool
contract description (definition) and the executor (runtime). It lives here
so definition.py never imports runtime.py for one integer.
"""

from __future__ import annotations

MAX_REPLACEMENTS = 8
SEARCH_PAGE_MAX_RESULTS = 100
PAGE_DETAIL_TOOL = "inspect_page_detail"
ARGUMENT_CONTENT_GUIDANCE = "Preserve argument strings exactly, including whether quote characters are present."
FULL_READ_REFERENCE_HINT = (
    "Use the latest complete read_file receipt reference. The reference belongs to the original outer Result; "
    "unwrap returns only file data. CodeMode example: const base=await tools.read_file({path:inputs.path}); "
    "const data=unwrap(base); then pass base.result_ref as base_result_ref."
)
STALE_READ_REFERENCE_CODE = "stale_read_reference"
STALE_READ_REFERENCE_ERROR = (
    "file version changed since the referenced full read. Read the current file again with read_file "
    "before retrying; the old receipt cannot authorize this write. " + FULL_READ_REFERENCE_HINT
)
EXACT_REPLACEMENT_CONTEXT_HINT = (
    "Copy old_string exactly, including indentation and whitespace. "
    "Include surrounding lines and the target text together in old_string of the same replacement "
    "so it matches one location uniquely. Preserve those surrounding lines in new_string. "
    "Separate replacement objects do not scope one another. "
    "Use the returned current context to refine the replacement; if insufficient, reread the file. "
    "Do not repeat the failed search unchanged. "
    "For a difficult multiline change, use content with the complete read_file base_result_ref, "
    "preserving unrelated text."
)

SEARCH_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".next",
    ".turbo",
    "target",
}
