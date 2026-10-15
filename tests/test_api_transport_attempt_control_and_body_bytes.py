"""Transport cancellation and silence accounting operate on bytes."""

import time

import pytest

from codey.providers.api_transport import AttemptControl, BoundedBodyReader


@pytest.mark.parametrize("failure", [OSError("socket interrupted"), ValueError("closed response")])
@pytest.mark.parametrize("cancelled", [True, False])
def test_body_read_failure_is_classified_as_cancelled_only_after_stop(failure, cancelled):
    from codey.providers.api_transport import GenerationUnknownError

    control = AttemptControl()

    class Source:
        def read(self, _size):
            control.cancelled = cancelled
            raise failure

    reader = BoundedBodyReader(Source(), control=control)
    if cancelled:
        with pytest.raises(GenerationUnknownError, match="cancelled") as caught:
            reader.read_chunk()
        assert caught.value.__cause__ is failure
    else:
        with pytest.raises(type(failure)) as caught:
            reader.read_chunk()
        assert caught.value is failure


def test_attempt_control_cancels_registered_connection():
    class Socket:
        def __init__(self):
            self.closed = False
        def shutdown(self, _how):
            self.closed = True

    control = AttemptControl()
    sock = Socket()
    control.register_connection(sock)
    control.cancel()
    assert control.cancelled and sock.closed


def test_body_reader_updates_activity_for_partial_bytes():
    class Source:
        def read(self, _size):
            return b"x"
    control = AttemptControl()
    reader = BoundedBodyReader(Source(), control=control, deadline=time.monotonic() + 1)
    before = control.last_byte_at
    assert reader.read_chunk() == b"x"
    assert control.last_byte_at >= before


def test_body_reader_scans_long_sse_line_incrementally():
    class Source:
        def __init__(self):
            self.body = b"x" * 100_000 + b"\n"
            self.calls = 0
        def read(self, size):
            self.calls += 1
            chunk, self.body = self.body[:size], self.body[size:]
            return chunk
    source = Source()
    line = BoundedBodyReader(source).readline()
    assert len(line) == 100_001
    assert source.calls < 100


def test_sse_terminal_event_returns_before_http_body_eof():
    from codey.providers.api_transport import read_sse

    class Source:
        def __init__(self):
            self.body = (b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
                         b'data: [DONE]\n\n' + b"x" * 100000)
        def read1(self, size):
            chunk, self.body = self.body[: min(size, 17)], self.body[min(size, 17):]
            return chunk
    class Response(Source):
        def readline(self, size=-1):
            return self.read1(size)
    result = read_sse(Response(), deadline=time.monotonic() + 1, cancelled=lambda: False)
    assert result["choices"][0]["message"]["content"] == "ok"
