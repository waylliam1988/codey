"""Shared toolchain constants owned below the definition/runtime layers.

MAX_REPLACEMENTS is the single edit-fanout bound used both by the tool
contract description (definition) and the executor (runtime). It lives here
so definition.py never imports runtime.py for one integer.
"""

from __future__ import annotations


MAX_REPLACEMENTS = 8
