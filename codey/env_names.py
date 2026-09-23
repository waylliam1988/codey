"""Brand-free environment variable names (single source of truth).

Import these constants instead of hardcoding ``os.environ`` keys, so a
future project rename touches this file plus the lock test only. No
``CODEY_``-prefixed name may be introduced anywhere else in the repo.
"""

from __future__ import annotations

NATIVE_TOOLS_ENV = "NATIVE_TOOLS"
BROWSER_PATH_ENV = "BROWSER_PATH"
LOCAL_OPENAI_BASE_URL_ENV = "LOCAL_OPENAI_BASE_URL"
LOCAL_OPENAI_MODEL_ENV = "LOCAL_OPENAI_MODEL"
LOCAL_OPENAI_API_KEY_ENV = "LOCAL_OPENAI_API_KEY"
PROVIDER_WORKER_CHILD_ENV = "PROVIDER_WORKER_CHILD"
PROVIDER_CDP_PORT_ENV = "PROVIDER_CDP_PORT"
RUN_BROWSER_E2E_ENV = "RUN_BROWSER_E2E"

APP_VERSION_PLACEHOLDER = "__APP_VERSION__"

__all__ = [
    "APP_VERSION_PLACEHOLDER",
    "BROWSER_PATH_ENV",
    "LOCAL_OPENAI_API_KEY_ENV",
    "LOCAL_OPENAI_BASE_URL_ENV",
    "LOCAL_OPENAI_MODEL_ENV",
    "NATIVE_TOOLS_ENV",
    "PROVIDER_CDP_PORT_ENV",
    "PROVIDER_WORKER_CHILD_ENV",
    "RUN_BROWSER_E2E_ENV",
]
