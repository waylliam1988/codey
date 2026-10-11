"""Local /models discovery rejects malformed model identifiers."""

from __future__ import annotations

import json
import urllib.request
from typing import Literal

import pytest

from codey.providers import local_discovery as discovery


class _ModelsResponse:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _ModelsResponse:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        return False

    def read(self, size: int | None = None) -> bytes:
        assert size == discovery.MODELS_RESPONSE_MAX_BYTES + 1
        return self._body


def test_models_probe_rejects_non_string_model_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: _ModelsResponse({"data": [{"id": {"name": "bad"}}]}),
    )

    endpoint, reason = discovery.probe_local_endpoint_detail("http://127.0.0.1:9/v1")

    assert endpoint is None
    assert reason == "invalid_json"
