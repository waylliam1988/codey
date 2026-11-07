"""Invoke explicit production provider actions in storage-boundary tests."""
from __future__ import annotations

from codey.operations.kernel_driver import drive_provider_request
from codey.operations.provider_request import prepare_provider_request as prepare_typed_request
from codey.providers.request_codec import decode_provider_request
from codey.providers.response_record import json_value


def prepare_legacy_request(sink, method, args, kwargs, *, counters):
    request = decode_provider_request({"method": method, "args": json_value(args), "kwargs": json_value(kwargs)})
    return prepare_typed_request(sink, request, counters=counters)


class ProviderActions:
    def __init__(self, provider, sink):
        self.provider, self.sink = provider, sink

    def send(self, *args, **kwargs):
        return self.invoke("send", *args, **kwargs)

    def invoke(self, method, *args, **kwargs):
        reference = prepare_legacy_request(self.sink, method, args, kwargs, counters=None)
        return drive_provider_request(self.sink, self.provider, reference)
