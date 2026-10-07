"""Static provider catalog: ids, labels, and worker ports.

Import-cheap by construction: stdlib only, no browser/driver/worker imports,
so ``codey --help`` and ``provider_ids()`` never pay the Playwright tax.
Connection logic lives in :mod:`codey.providers.registry`, which imports this
module (never the reverse).
"""

from __future__ import annotations

from codey.env_names import PROVIDER_WORKER_CHILD_ENV

DEFAULT_PROVIDER_ID = "deepseek"
API_CONNECTIONS = {
    "local": ("Local", "codey.providers.local_connection"),
    "zen": ("OpenCode Zen", "codey.providers.zen.connection"),
}
PROVIDER_LABELS = {
    "deepseek": "DeepSeek",
    "mimo": "MiMo",
    "stepfun": "StepFun",
    "qwen": "Qwen",
    "glm": "GLM",
    **{key: registration[0] for key, registration in API_CONNECTIONS.items()},
}
WEB_PROVIDER_LABELS = {
    key: label for key, label in PROVIDER_LABELS.items() if key not in API_CONNECTIONS
}
PROVIDER_WORKER_PORT_OFFSETS = {
    "deepseek": 101,
    "mimo": 102,
    "qwen": 103,
    "glm": 104,
    "stepfun": 105,
}
WORKER_CHILD_ENV = PROVIDER_WORKER_CHILD_ENV


def provider_ids() -> tuple[str, ...]:
    return tuple(PROVIDER_LABELS)


def display_provider_name(explicit_id: object, provider: object) -> str:
    """Display-only provider label; never a durable identity.

    Durable kernel paths require an explicit ``provider_id`` and fail when
    it is empty. This helper only resolves the human-readable label for
    conversation windows and test traces.
    """
    try:
        text = str(explicit_id or "").strip()
        if text:
            return text
    except Exception:
        pass
    try:
        return str(getattr(provider, "name", "") or "")
    except Exception:
        return ""


__all__ = [
    "DEFAULT_PROVIDER_ID",
    "PROVIDER_LABELS",
    "PROVIDER_WORKER_PORT_OFFSETS",
    "WEB_PROVIDER_LABELS",
    "WORKER_CHILD_ENV",
    "display_provider_name",
    "provider_ids",
]
