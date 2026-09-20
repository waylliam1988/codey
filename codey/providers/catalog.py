"""Static provider catalog: ids, labels, and worker ports.

Import-cheap by construction: stdlib only, no browser/driver/worker imports,
so ``codey --help`` and ``provider_ids()`` never pay the Playwright tax.
Connection logic lives in :mod:`codey.providers.registry`, which imports this
module (never the reverse).
"""

from __future__ import annotations

DEFAULT_PROVIDER_ID = "deepseek"
PROVIDER_LABELS = {
    "deepseek": "DeepSeek",
    "mimo": "MiMo",
    "stepfun": "StepFun",
    "qwen": "Qwen",
    "glm": "GLM",
    "local": "Local",
}
WEB_PROVIDER_LABELS = {
    key: label for key, label in PROVIDER_LABELS.items() if key != "local"
}
PROVIDER_WORKER_PORT_OFFSETS = {
    "deepseek": 101,
    "mimo": 102,
    "qwen": 103,
    "glm": 104,
    "stepfun": 105,
}
WORKER_CHILD_ENV = "CODEY_PROVIDER_WORKER_CHILD"


def provider_ids() -> tuple[str, ...]:
    return tuple(PROVIDER_LABELS)


__all__ = [
    "DEFAULT_PROVIDER_ID",
    "PROVIDER_LABELS",
    "PROVIDER_WORKER_PORT_OFFSETS",
    "WEB_PROVIDER_LABELS",
    "WORKER_CHILD_ENV",
    "provider_ids",
]
