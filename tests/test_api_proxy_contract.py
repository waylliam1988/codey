"""Proxy protocol and authentication isolation contracts."""

import pytest

from codey.providers.api_http import ApiHttpSession
from codey.providers.api_provider import ApiProvider
from codey.providers.error_classification import RequestPrepError


@pytest.mark.parametrize("proxy", ["https://127.0.0.1:9", "socks5://127.0.0.1:9", "proxy+unknown://host"])
@pytest.mark.parametrize("pooled", [False, True])
def test_unsupported_proxy_scheme_is_rejected_before_any_socket(proxy: str, pooled: bool) -> None:
    provider = ApiProvider(
        "http://127.0.0.1:1/v1",
        "fixture",
        timeout=1,
        http_session=ApiHttpSession() if pooled else None,
        transport_options={"proxy": proxy},
    )
    try:
        with pytest.raises(RequestPrepError, match="unsupported proxy scheme"):
            provider.send("hello")
    finally:
        provider.close()
