"""Single owner for kernel recovery/settlement failures (no cycles).

``RecoveryFailed`` means a recovered result could not be delivered; the run
must stop without invoking new tools. ``EffectSettlementFailed`` means the
durable receipt write failed after execution; the batch must stop and the
outer loop maps it to ``provider_failure``/``recovery_failure``.
"""

from __future__ import annotations

__all__ = [
    "EffectSettlementFailed",
    "RecoveryFailed",
]


class RecoveryFailed(RuntimeError):
    """Recovered tool results could not be delivered; the run must stop."""


class EffectSettlementFailed(RuntimeError):
    """Durable effect receipt could not be settled; fail closed."""
