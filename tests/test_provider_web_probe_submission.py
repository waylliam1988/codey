"""A rejected background submission must not permanently suppress later status probes."""

from concurrent.futures import Future
from pathlib import Path

import pytest

from codey.app import provider_services
from codey.app.context import AppContext


def _probe_running() -> bool:
    with provider_services._WEB_PROBE_LOCK:
        return provider_services._WEB_PROBE_RUNNING


def _status(ctx: AppContext, provider_id: str) -> dict[str, object]:
    for row in provider_services.provider_status_payload(ctx):
        if row["id"] == provider_id:
            return row
    raise AssertionError(f"provider missing from snapshot: {provider_id}")


def test_a_failed_submission_leaves_the_next_request_eligible_to_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[set[str]] = []

    def submit(operation: object, *, allowed: set[str]) -> Future[dict[str, bool]]:
        assert callable(operation)
        attempts.append(set(allowed))
        if len(attempts) == 1:
            raise RuntimeError("can't start new thread")
        future: Future[dict[str, bool]] = Future()
        future.set_result({provider_id: provider_id == "deepseek" for provider_id in allowed})
        return future

    monkeypatch.setattr(provider_services._WEB_PROBE_EXECUTOR, "submit", submit)
    monkeypatch.setattr(provider_services, "_WEB_PROBE_RUNNING", False)
    provider_services.reset_provider_availability_cache()
    ctx = AppContext(tmp_path, start_scheduler=False, memory_enabled=False)
    try:
        assert _status(ctx, "deepseek")["state"] == "checking"
        _status(ctx, "deepseek")
        assert len(attempts) == 2
        assert _status(ctx, "deepseek")["available"] is True
        assert _probe_running() is False
    finally:
        ctx.close()
        provider_services.reset_provider_availability_cache()


def test_pending_and_failed_probes_release_the_slot_without_duplicate_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[set[str]] = []
    futures: list[Future[dict[str, bool]]] = []

    def submit(operation: object, *, allowed: set[str]) -> Future[dict[str, bool]]:
        assert callable(operation)
        attempts.append(set(allowed))
        future: Future[dict[str, bool]] = Future()
        futures.append(future)
        return future

    monkeypatch.setattr(provider_services._WEB_PROBE_EXECUTOR, "submit", submit)
    monkeypatch.setattr(provider_services, "_WEB_PROBE_RUNNING", False)
    provider_services.reset_provider_availability_cache()
    ctx = AppContext(tmp_path, start_scheduler=False, memory_enabled=False)
    try:
        assert _status(ctx, "deepseek")["state"] == "checking"
        assert _status(ctx, "deepseek")["state"] == "checking"
        assert len(attempts) == 1
        assert _probe_running() is True
        futures[0].set_exception(RuntimeError("browser unavailable"))
        assert _probe_running() is False
        provider_services.reset_provider_availability_cache()
        _status(ctx, "deepseek")
        assert len(attempts) == 2
        futures[1].set_result({provider_id: False for provider_id in attempts[1]})
        assert _status(ctx, "deepseek")["state"] == "unavailable"
        assert _probe_running() is False
    finally:
        ctx.close()
        provider_services.reset_provider_availability_cache()
