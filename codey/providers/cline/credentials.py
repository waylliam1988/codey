"""Atomic Windows Credential Manager records; no plaintext fallback."""
from __future__ import annotations

import ctypes
import json
import math
import sys
from ctypes import wintypes
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from codey.providers.connection_controls import ConnectionActionError


@dataclass(frozen=True, repr=False)
class CredentialRecord:
    access_token: str
    refresh_token: str
    expires_at: float
    account_epoch: str
    token_version: int = 0

    def __post_init__(self) -> None:
        if (any(not isinstance(value, str) or not value.strip() or len(value) > 16000
                for value in (self.access_token, self.refresh_token, self.account_epoch))
                or type(self.expires_at) not in (int, float) or not math.isfinite(self.expires_at) or self.expires_at <= 0
                or type(self.token_version) is not int or self.token_version < 0):
            raise ValueError("invalid credential record")


class TokenStore(Protocol):
    def load(self) -> CredentialRecord | None: ...
    def save(self, record: CredentialRecord) -> None: ...
    def delete(self) -> None: ...


class _Credential(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


def _storage_error() -> ConnectionActionError:
    return ConnectionActionError("auth_storage_failed", "Cannot access the operating system credential store.", 503)


class WindowsTokenStore:
    def __init__(self, target: str = "Codey/Cline/default") -> None:
        self.target = target

    def _api(self) -> Any:
        if sys.platform != "win32":
            raise _storage_error()
        api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(ctypes.POINTER(_Credential))]
        api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
        api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        api.CredFree.argtypes = [ctypes.c_void_p]
        return api

    def load(self) -> CredentialRecord | None:
        api = self._api()
        ptr = ctypes.POINTER(_Credential)()
        if not api.CredReadW(self.target, 1, 0, ctypes.byref(ptr)):
            if ctypes.get_last_error() == 1168:
                return None
            raise _storage_error()
        try:
            value = json.loads(ctypes.string_at(ptr.contents.CredentialBlob,
                                               ptr.contents.CredentialBlobSize).decode("utf8"))
            record = CredentialRecord(**value)
            if (not record.access_token or not record.refresh_token or not record.account_epoch
                    or type(record.token_version) is not int or record.expires_at <= 0):
                raise ValueError("invalid credential record")
            return record
        except (ValueError, TypeError, KeyError):
            raise _storage_error() from None
        finally:
            api.CredFree(ptr)

    def save(self, record: CredentialRecord) -> None:
        blob = json.dumps(asdict(record), separators=(",", ":")).encode("utf8")
        if len(blob) > 2560:
            raise _storage_error()
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential = _Credential(Type=1, TargetName=self.target, CredentialBlobSize=len(blob),
                                 CredentialBlob=buffer, Persist=2, UserName="Codey")
        if not self._api().CredWriteW(ctypes.byref(credential), 0):
            raise _storage_error()

    def delete(self) -> None:
        if not self._api().CredDeleteW(self.target, 1, 0) and ctypes.get_last_error() != 1168:
            raise _storage_error()
