"""Web adapter repair surface: per-provider drivers plus shared web files.

Adapter self-repair exists to revive a broken web chat surface, so its
allowlist is "everything web-adapter shaped", not "one provider file":

- per provider: the site-specific driver module;
- shared: the wrapper, send plumbing, profiles, and browser helpers every
  web provider depends on.

Everything outside this surface -- task_runner, server, tool_runtime,
evidence, completion, permission, tests -- stays forbidden: repair must
never become Codey runtime self-modification. Because repairs install into
a per-provider override root (``PYTHONPATH=override.root``), touching the
shared files inside one provider's override cannot leak into another
provider's runtime path; it only widens what that repair's own validation
must prove.
"""

from __future__ import annotations


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
    "codey/provider_profiles.py",
    "codey/provider_profiles.json",
    "codey/provider_controls.py",
    "codey/provider_flow.py",
    "codey/provider_send_loop.py",
    "codey/provider_submission.py",
    "codey/provider_timeouts.py",
    "codey/web_clipboard.py",
    "codey/browser.py",
)


def driver_files(provider_id: str) -> tuple[str, ...]:
    return PROVIDER_DRIVER_FILES.get(str(provider_id or "").strip().lower(), ())


def shared_web_adapter_files() -> tuple[str, ...]:
    return SHARED_WEB_ADAPTER_FILES


def adapter_repair_surface(provider_id: str) -> tuple[str, ...]:
    """Every file one provider's self-repair may replace."""

    return (*driver_files(provider_id), *SHARED_WEB_ADAPTER_FILES)
