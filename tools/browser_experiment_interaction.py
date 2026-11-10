"""Compatibility import for the production browser session.

The live experiment keeps its historical import path, but the interaction
implementation is owned by ``codey.automation`` so experiments cannot create
a second browser runtime.
"""

from codey.automation.browser_session import BrowserSession as BrowserInteraction
from codey.automation.browser_session import BrowserToolOutput

__all__ = ["BrowserInteraction", "BrowserToolOutput"]
