import subprocess
import time

import pytest

from codey.automation.browser_contracts import BrowserAction, BrowserCommand, BrowserConfig
from codey.automation.browser_ipc import BrowserPacket
from codey.automation.browser_process import BrowserProcess
from codey.types.json_value import decode_json_object


def test_failed_worker_init_reaps_child_and_allows_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = BrowserProcess(BrowserConfig(), "task", "resource")
    send = worker._send
    failed_processes: list[subprocess.Popen[bytes]] = []

    def fail_first_init(packet: BrowserPacket, *, expected: subprocess.Popen[bytes] | None = None) -> None:
        if packet.message.get("type") == "init" and not failed_processes:
            assert worker._process is not None
            failed_processes.append(worker._process)
            raise OSError("test worker init write failed")
        send(packet, expected=expected)

    monkeypatch.setattr(worker, "_send", fail_first_init)
    try:
        failed = worker.call(
            BrowserCommand("first", BrowserAction.DOCUMENT, {}, "task", "resource", "", time.monotonic() + 10),
        )
        row = decode_json_object(failed.message["value"])
        observation = decode_json_object(row["observation"])
        assert observation["delivery"] == "not_delivered"
        assert "test worker init write failed" in str(observation["reason"])
        assert len(failed_processes) == 1
        assert failed_processes[0].poll() is not None
        assert worker._process is None

        retried = worker.call(
            BrowserCommand("next", BrowserAction.DOCUMENT, {}, "task", "resource", "", time.monotonic() + 10),
        )
        assert retried.message["completion"] == "completed"
        assert worker._process is not failed_processes[0]
    finally:
        worker.close(release=False)
