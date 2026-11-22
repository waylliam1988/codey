"""An active Writer's stop must not become a normal provider failover order."""

import pytest

from codey.app.provider_registry import ProviderRegistry
from codey.runtime.core.cancellation import TaskCancelled


def test_user_stop_propagates_from_failover_availability_probe() -> None:
    registry = ProviderRegistry()

    def cancelled() -> dict[str, bool]:
        raise TaskCancelled("task stopped")

    try:
        with pytest.raises(TaskCancelled, match="task stopped"):
            registry.failover_order(cancelled)
    finally:
        registry.connection_status.close()


def test_ordinary_probe_failure_keeps_the_configured_fallback_order() -> None:
    registry = ProviderRegistry()

    def offline() -> dict[str, bool]:
        raise RuntimeError("browser status unavailable")

    try:
        assert registry.failover_order(offline) == ("deepseek", "mimo", "stepfun", "qwen", "glm")
    finally:
        registry.connection_status.close()
