"""URL policy for Research web reads."""

from __future__ import annotations

from codey.policies.network import DEFAULT_NETWORK_POLICY


def check_fetch_url(
    url: str,
    *,
    resolve: bool = True,
    use_cache: bool = False,
) -> str | None:
    return DEFAULT_NETWORK_POLICY.check_url(
        url,
        resolve=resolve,
        use_cache=use_cache,
    )
