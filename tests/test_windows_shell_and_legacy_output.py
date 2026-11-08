"""Host selection, byte decoding and persistence share observed facts."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from codey.env_names import SHELL_EXECUTABLE_ENV
from codey.runtime.core import shell_discovery
from codey.runtime.core.output_capture import BoundedByteCapture, CapturedText
from codey.storage.execution_output_store import (
    SCHEMA,
    ExecutionOutputObserver,
    ExecutionOutputStore,
    ExecutionSnapshot,
)


@pytest.mark.parametrize(("available", "explicit", "expected"), [
    (("pwsh", "powershell", "cmd"), "", "pwsh"),
    (("powershell", "cmd"), "", "powershell"),
    (("cmd",), "", "cmd"),
    (("pwsh", "cmd"), "cmd", "cmd"),
])
def test_windows_discovery_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                 available: tuple[str, ...], explicit: str, expected: str) -> None:
    paths: dict[str, str] = {}
    for name in available:
        binary = tmp_path / f"{name}.exe"
        binary.touch()
        paths[name] = str(binary)

    def which(name: str, *, path: str | None = None) -> str | None:
        return paths.get(name)

    monkeypatch.setattr(shell_discovery.shutil, "which", which)
    monkeypatch.setattr(shell_discovery, "_observe", lambda *args: ("7.0", "0" * 64))
    kind, executable, _, _ = shell_discovery.discover_shell("win32", {
        SHELL_EXECUTABLE_ENV: explicit, "COMSPEC": paths["cmd"], "PATH": str(tmp_path)})
    assert executable == paths[expected]
    assert kind == ("cmd" if expected == "cmd" else "powershell")


def test_invalid_explicit_shell_never_uses_default(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unavailable"):
        shell_discovery.discover_shell("win32", {SHELL_EXECUTABLE_ENV: str(tmp_path / "pwsh.exe")})


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 1000])
def test_gbk_is_decoded_once_with_host_fallback(chunk_size: int) -> None:
    raw = "中文错误消息：文件不存在".encode("gbk")
    capture = BoundedByteCapture(fallback_encoding="gbk")
    for offset in range(0, len(raw), chunk_size):
        capture.feed(raw[offset:offset + chunk_size])
        capture.finish(final=False)
    result = capture.finish()
    assert result.encoding == "gbk" and not result.decoding_errors
    assert result.head == "中文错误消息：文件不存在"
    assert result.total_bytes == len(raw) and result.omitted_bytes == 0
    assert "host code page" in result.text
    assert capture.finish() == result


@pytest.mark.parametrize(("head_limit", "tail_limit"), [(1, 1), (3, 6), (8, 7), (0, 4)])
def test_dbcs_edges_use_original_character_boundaries(head_limit: int, tail_limit: int) -> None:
    text = "丂中a文" * 50  # GBK 0x81 0x40 has an ASCII-looking trail byte.
    raw = text.encode("gbk")
    capture = BoundedByteCapture(head_limit=head_limit, tail_limit=tail_limit, fallback_encoding="gbk")
    for offset in range(0, len(raw), 3):
        capture.feed(raw[offset:offset + 3])
    result = capture.finish()
    expected_head, expected_tail = "", ""
    offset = 0
    for char in text:
        end = offset + len(char.encode("gbk"))
        if end <= head_limit:
            expected_head += char
        if offset >= len(raw) - tail_limit:
            expected_tail += char
        offset = end
    assert result.head == expected_head and result.tail == expected_tail
    assert result.omitted_bytes == len(raw) - len((expected_head + expected_tail).encode("gbk"))


def test_observer_and_capture_agree_after_slow_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExecutionOutputStore(tmp_path, max_bytes=4096)
    store.create("e", session_id="s", run_id="r")
    entered, release = threading.Event(), threading.Event()
    replace = store.replace_capture

    def stalled(execution_id: str, channel: str, capture: CapturedText) -> ExecutionSnapshot:
        entered.set()
        assert release.wait(5)
        return replace(execution_id, channel, capture)

    monkeypatch.setattr(store, "replace_capture", stalled)
    observer = ExecutionOutputObserver(store, "e", fallback_encoding="gbk")
    raw = ("中文错误" * 100).encode("gbk")
    try:
        observer.offer("stdout", raw[:1])
        assert entered.wait(5)
        for byte in raw[1:]:
            observer.offer("stdout", bytes([byte]))
    finally:
        release.set()
        observer.close()
    expected = BoundedByteCapture(head_limit=1024, tail_limit=3072, fallback_encoding="gbk")
    expected.feed(raw)
    stream = store.settled("e", 7)["stdout"]
    assert stream["text"] == expected.finish().text
    assert stream["total_bytes"] == len(raw)
    assert not stream["truncated"] and stream["encoding"] == "gbk"
    store.close()
    restored = ExecutionOutputStore(tmp_path)
    assert restored.snapshot("e")["stdout"] == stream
    restored.close()


def test_existing_database_is_upgraded_without_changing_output(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "execution_outputs.sqlite3") as database:
        database.executescript(SCHEMA)
        database.execute("INSERT INTO execution_outputs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         ("e", "s", "r", "settled", 1, "hello", "", 5, 0, 7, 100))
    store = ExecutionOutputStore(tmp_path)
    assert store.snapshot("e")["stdout"]["text"] == "hello"
    assert store.snapshot("e")["exit_code"] == 7
    store.close()


def test_failed_observation_does_not_become_complete_on_process_exit(tmp_path: Path) -> None:
    store = ExecutionOutputStore(tmp_path)
    store.create("e", session_id="s", run_id="r")
    store.unavailable("e")
    assert store.settled("e", 0)["execution_state"] == "unknown"
    store.close()


def test_native_run_projects_process_scope_with_its_observed_code(tmp_path: Path) -> None:
    from codey.toolchain.runtime import project_run_command_result, run_command_raw

    raw = run_command_raw(tmp_path, ".", "python -m py_compile missing.py", permission_profile="coding_writer")
    assert raw.exit_code == 1
    result = project_run_command_result(tmp_path, raw)
    assert result.audit["exit_code_scope"] == "process"
    assert "Exit code scope: process" in result.model_text
    execution = result.canonical["execution"]
    assert isinstance(execution, dict)
    assert execution["status"] == "exit" and execution["exit_code"] == 1
    assert execution["exit_code_scope"] == "process" and execution["full_output_recoverable"] is True
