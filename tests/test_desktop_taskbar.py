"""Windows taskbar grouping must identify Codey independently of Python."""

from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest

from codey.app import desktop


class SetAppIdentity:
    def __init__(self) -> None:
        self.argtypes: list[type[ctypes.c_wchar_p]] = []
        self.calls: list[str] = []

    def __call__(self, identity: str) -> None:
        self.calls.append(identity)


class Shell:
    def __init__(self, setter: SetAppIdentity) -> None:
        self.SetCurrentProcessExplicitAppUserModelID = setter


def test_windows_desktop_sets_codey_process_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    setter = SetAppIdentity()

    def library(name: str) -> Shell:
        assert name == 'shell32'
        return Shell(setter)

    monkeypatch.setattr(desktop, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(ctypes, 'OleDLL', library, raising=False)
    desktop._set_taskbar_identity()
    assert setter.argtypes == [ctypes.c_wchar_p]
    assert setter.calls == ['Codey.Desktop']


@pytest.mark.parametrize('platform', ['linux', 'darwin'])
def test_other_platforms_do_not_load_windows_libraries(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    def library(name: str) -> Shell:
        raise AssertionError(f'Windows library loaded on {platform}: {name}')

    monkeypatch.setattr(desktop, 'sys', SimpleNamespace(platform=platform))
    monkeypatch.setattr(ctypes, 'OleDLL', library, raising=False)
    desktop._set_taskbar_identity()


def test_taskbar_identity_failure_is_not_silently_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    class RejectedIdentity(SetAppIdentity):
        def __call__(self, identity: str) -> None:
            raise OSError(f'Windows rejected {identity}')

    def library(name: str) -> Shell:
        return Shell(RejectedIdentity())

    monkeypatch.setattr(desktop, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(ctypes, 'OleDLL', library, raising=False)
    with pytest.raises(OSError, match='Windows rejected Codey.Desktop'):
        desktop._set_taskbar_identity()
