"""Web adapter repair surface: per-provider drivers plus shared web files.

Adapter self-repair exists to revive a broken web chat surface, so its
allowlist is "everything web-adapter shaped", not "one provider file":

- per provider: the site-specific driver module;
- shared: the wrapper, send plumbing, profiles, and browser helpers every
  web provider depends on.

Everything outside this surface -- task service, server, tool_runtime,
evidence, completion, permission, tests -- stays forbidden: repair must
never become Codey runtime self-modification. Because repairs install into
a per-provider override root (``PYTHONPATH=override.root``), touching the
shared files inside one provider's override cannot leak into another
provider's runtime path; it only widens what that repair's own validation
must prove.

The surface is fail-closed: a provider without a driver entry has an empty
surface. The shared files are never granted on their own -- they only ever
widen a known provider's driver surface.
"""

from __future__ import annotations

from codey.providers.ids import normalize_provider_id


PROVIDER_DRIVER_FILES = {
    "deepseek": ("codey/providers/web_drivers/deepseek.py",),
    "qwen": ("codey/providers/web_drivers/qwen.py",),
    "mimo": ("codey/providers/web_drivers/mimo.py",),
    "stepfun": ("codey/providers/web_drivers/stepfun.py",),
    "glm": ("codey/providers/web_drivers/glm.py",),
}

SHARED_WEB_ADAPTER_FILES = (
    "codey/providers/web_provider.py",
    "codey/providers/web_driver.py",
    "codey/providers/web_drivers/common.py",
    "codey/providers/profiles.py",
    "codey/providers/profiles.json",
    "codey/providers/controls.py",
    "codey/providers/flow.py",
    "codey/providers/send_loop.py",
    "codey/providers/submission.py",
    "codey/providers/timeouts.py",
    "codey/automation/web_clipboard.py",
    "codey/automation/browser.py",
)


def driver_files(provider_id: str) -> tuple[str, ...]:
    return PROVIDER_DRIVER_FILES.get(normalize_provider_id(provider_id), ())


def adapter_repair_surface(provider_id: str) -> tuple[str, ...]:
    """Every file one provider's self-repair may replace.

    Empty for unknown providers: the shared web files are granted only in
    combination with a real driver surface, never alone.
    """

    driver = driver_files(provider_id)
    if not driver:
        return ()
    return (*driver, *SHARED_WEB_ADAPTER_FILES)
