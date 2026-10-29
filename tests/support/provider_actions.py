"""Invoke explicit production provider actions in storage-boundary tests."""
from __future__ import annotations

from codey.operations.kernel_driver import drive_provider_request, prepare_provider_request


class ProviderActions:
    def __init__(self, provider, sink):
        self.provider, self.sink = provider, sink

    def send(self, *args, **kwargs):
        return self.invoke("send", *args, **kwargs)

    def invoke(self, method, *args, **kwargs):
        reference = prepare_provider_request(self.sink, method, args, kwargs, counters=None)
        return drive_provider_request(self.sink, self.provider, reference)
