"""Browser boot exchanges its credential before requesting application state."""

from __future__ import annotations

from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "frontend/src/app/app_runtime.ts"


def test_real_ui_boot_awaits_operator_auth_before_catalog_or_sse() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    boot = source[source.index("async function boot()"):]
    assert boot.index("await OperatorAuth.authorize()") < boot.index("ProviderUI.adoptCatalog()")
    assert boot.index("await OperatorAuth.authorize()") < boot.index("connectEvents();")


def test_expired_bootstrap_and_concurrent_exchange_are_fail_closed() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from codey.app.operator_auth import BOOTSTRAP_LIFETIME_SECONDS, OperatorAuth

    now = [0.0]
    auth = OperatorAuth(1234, clock=lambda: now[0])
    token = auth.issue_bootstrap()
    now[0] = BOOTSTRAP_LIFETIME_SECONDS
    assert auth.exchange(token) is None
    token = auth.issue_bootstrap()
    tokens = [token] * 4
    with ThreadPoolExecutor(max_workers=4) as pool:
        results: list[str | None] = list(pool.map(auth.exchange, tokens))
    assert sum(cookie is not None for cookie in results) == 1
