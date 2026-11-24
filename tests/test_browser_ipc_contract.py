from io import BytesIO

import pytest

from codey.automation.browser_ipc import BrowserPacket, read_packet, write_packet


class _ShortWriter:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.buffer = BytesIO()
        self.flushed = False

    def write(self, data: bytes | memoryview, /) -> int:
        return self.buffer.write(data[:self.limit])

    def flush(self) -> None:
        self.flushed = True


@pytest.mark.parametrize("limit", [1, 6, 1024])
def test_packet_round_trips_after_short_writes(limit: int) -> None:
    packet = BrowserPacket({"type": "result", "text": "測試"}, b"\x00\xff\x01")
    writer = _ShortWriter(limit)
    write_packet(writer, packet)
    assert writer.flushed
    writer.buffer.seek(0)
    assert read_packet(writer.buffer) == packet
    assert writer.buffer.read() == b""


def test_packet_write_with_no_progress_fails() -> None:
    writer = _ShortWriter(0)
    with pytest.raises(OSError, match="browser worker pipe"):
        write_packet(writer, BrowserPacket({"type": "ready"}))
    assert not writer.flushed
