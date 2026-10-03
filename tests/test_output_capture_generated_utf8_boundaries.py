"""Capture invariants compare against independent UTF-8 slices and byte counts."""
from random import Random

import pytest

from codey.runtime.core.output_capture import BoundedByteCapture


@pytest.mark.parametrize("seed", range(40))
def test_chunk_boundaries_do_not_change_head_tail_or_byte_accounting(seed):
    rng = Random(seed)
    raw = ("汉🙂aé" * rng.randrange(20, 80)).encode("utf-8")
    head, tail = rng.randrange(0, 31), rng.randrange(0, 31)
    capture = BoundedByteCapture(head_limit=head, tail_limit=tail)
    at = 0
    while at < len(raw):
        size = rng.randrange(1, 13)
        capture.feed(raw[at:at + size])
        at += size
    result = capture.finish()
    expected_head = raw[:head].decode("utf-8", errors="ignore")
    expected_tail = raw[-tail:].decode("utf-8", errors="ignore") if tail else ""
    omitted = len(raw) - len(expected_head.encode()) - len(expected_tail.encode())
    assert result.total_bytes == len(raw)
    assert result.truncated is True
    assert result.omitted_bytes == omitted
    assert result.text == expected_head + f"\n[... omitted {omitted} bytes of output; showing head and tail only ...]\n" + expected_tail
    assert "�" not in result.text
