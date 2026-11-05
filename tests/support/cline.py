"""Cline fixtures never access a production credential or network endpoint."""
import copy
import io
import json
import time
from dataclasses import replace

from codey.providers.cline.credentials import CredentialRecord
from codey.providers.cline.http import JsonReply
from codey.providers.cline.runtime import Runtime

MODEL = "cline-free/solar-mini4"


class MemoryStore:
    def __init__(self, record=None):
        self.record = record
        self.saved = []
        self.deleted = 0

    def load(self):
        return self.record

    def save(self, record):
        self.saved.append(record)
        self.record = record

    def delete(self):
        self.deleted += 1
        self.record = None


def credential(*, expires=None, epoch="account-A", version=0):
    return CredentialRecord("ACCESS_MARKER", "REFRESH_MARKER", time.time() + 3600 if expires is None else expires,
                            epoch, version)


def token_response(access="ACCESS_ROTATED", refresh="REFRESH_ROTATED"):
    return {"success": True, "data": {"accessToken": access, "refreshToken": refresh,
                                      "expiresAt": "2099-01-01T00:00:00Z"}}


class ScriptHTTP:
    def __init__(self):
        self.calls = []
        self.hook = None
        self.free = [{"id": MODEL, "name": "Solar Mini 4"}]
        self.capabilities = [{"id": "upstage/solar-mini4", "context_length": 32768,
            "top_provider": {"max_completion_tokens": 8192},
            "supported_parameters": ["tools", "reasoning"],
            "reasoning": {"mandatory": False, "supported_efforts": ["low", "medium", "high"]}}]

    def request(self, url, body=None, **kwargs):
        self.calls.append((url, copy.deepcopy(body), kwargs))
        if self.hook:
            result = self.hook(url, body, kwargs)
            if result is not None:
                return result
        if url.endswith("recommended-models"):
            return JsonReply(200, {"free": copy.deepcopy(self.free), "recommended": [{"id": "paid"}]})
        if url.endswith("/models"):
            return JsonReply(200, {"data": copy.deepcopy(self.capabilities)})
        if url.endswith("/device"):
            return JsonReply(200, {"device_code": "DEVICE_MARKER", "user_code": "USER_CODE_MARKER",
                "verification_uri_complete": "https://authkit.cline.bot/device?user_code=USER_CODE_MARKER",
                "expires_in": 300, "interval": 1})
        if url.endswith("/authenticate"):
            return JsonReply(200, {"access_token": "WORKOS_ACCESS", "refresh_token": "WORKOS_REFRESH"})
        return JsonReply(200, token_response())


def resource(tmp_path, *, signed_in=True):
    store, http = MemoryStore(credential() if signed_in else None), ScriptHTTP()
    instance = Runtime(tmp_path, store=store, http=http, browser=lambda _: True)
    instance.catalog.refresh()
    return instance


class Response(io.BytesIO):
    def __init__(self, body, *, sse=False):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.headers = {"Content-Type": "text/event-stream" if sse else "application/json"}


def completion(message=None):
    return {"choices": [{"finish_reason": "stop", "message": message or {"content": "received"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 8, "cost": 0}}


def replace_account(instance, epoch):
    instance.auth.store.save(replace(instance.auth.store.load(), account_epoch=epoch))
