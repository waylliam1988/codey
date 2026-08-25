"""Site-specific web chat drivers.

Each module owns one provider page's selectors and flow. They share the
scaffolding in :mod:`codey.providers.web_drivers.common`; the unified
wrapper lives one level up in :mod:`codey.providers.web_provider`.

Keep this package's ``__init__`` import-free: ``web_provider`` imports the
driver modules from inside the ``codey.providers`` package init, and an
eager re-export here would turn that shape into a hard cycle.
"""
