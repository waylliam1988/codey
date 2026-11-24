"""Structured task-browser transport; tool authorization stays with the kernel."""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from codey.types.json_value import (
    JsonObject,
    decode_json_object,
    require_json_boolean,
    require_json_string,
)

MAX_PENDING_COMMANDS = 64


class BrowserAction(StrEnum):
    SNAPSHOT = "snapshot"
    CLICK = "click"
    FILL = "fill"
    SCROLL = "scroll"
    WAIT = "wait"
    NAVIGATE = "navigate"
    EVALUATE = "evaluate"
    TAB_LIST = "tab_list"
    TAB_NEW = "tab_new"
    TAB_SWITCH = "tab_switch"
    UPLOAD = "upload"
    DOWNLOAD = "download"
    CAPTURE = "capture"
    DOCUMENT = "document"
    BINDING = "binding"
    RECORDS = "records"
    QUERY_RESULT = "query_result"
    MATCH_QUERY = "match_query"
    FRAME_LIST = "frame_list"
    FRAME_SELECT = "frame_select"
    LOCATE = "locate"
    INSPECT = "inspect"
    DISCLOSURE = "disclosure"
    PEEK = "peek"
    RESET_REFS = "reset_refs"
    PRESS = "press"
    HOVER = "hover"
    SELECT = "select"
    CHECK = "check"
    DIALOG = "dialog"
    DIALOG_RESPOND = "dialog_respond"
    SCREENSHOT = "screenshot"
    VISUAL_CLICK = "visual_click"


class BrowserCompletion(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class InputRelease(StrEnum):
    RELEASED = "released"
    PENDING = "pending"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BrowserToolOutput:
    model_text: str
    ok: bool
    observation: JsonObject
    binary: bytes = b""

    def payload(self) -> JsonObject:
        return {"model_text": self.model_text, "ok": self.ok, "observation": self.observation}

    @classmethod
    def read(cls, raw: object, *, binary: bytes = b"") -> BrowserToolOutput:
        row = decode_json_object(raw)
        if set(row) != {"model_text", "ok", "observation"}:
            raise ValueError("invalid browser result shape")
        return cls(require_json_string(row["model_text"], "model_text"),
                   require_json_boolean(row["ok"], "ok"), decode_json_object(row["observation"]), binary)


@dataclass(frozen=True)
class BrowserCommand:
    operation_id: str
    action: BrowserAction
    arguments: JsonObject
    owner_task_id: str
    resource_id: str
    effect_ref: str
    deadline: float
    target_id: str = ""
    frame_id: str = ""

    def payload(self) -> JsonObject:
        return {"operation_id": self.operation_id, "action": self.action.value, "arguments": self.arguments,
                "owner_task_id": self.owner_task_id, "resource_id": self.resource_id,
                "effect_ref": self.effect_ref, "deadline": self.deadline,
                "target_id": self.target_id, "frame_id": self.frame_id}

    @classmethod
    def read(cls, raw: object) -> BrowserCommand:
        row = decode_json_object(raw)
        if set(row) != {"operation_id", "action", "arguments", "owner_task_id", "resource_id", "effect_ref", "deadline",
                        "target_id", "frame_id"}:
            raise ValueError("invalid browser command shape")
        deadline = row["deadline"]
        if isinstance(deadline, bool) or not isinstance(deadline, int | float):
            raise ValueError("invalid browser command deadline")
        try:
            deadline = float(deadline)
        except OverflowError as exc:
            raise ValueError("invalid browser command deadline") from exc
        if not math.isfinite(deadline) or deadline <= 0:
            raise ValueError("invalid browser command deadline")
        values = {key: require_json_string(row[key], key)
                  for key in ("operation_id", "owner_task_id", "resource_id", "effect_ref")}
        if not all(values[key] for key in ("operation_id", "resource_id", "owner_task_id")):
            raise ValueError("missing browser invocation identity")
        return cls(values["operation_id"], BrowserAction(require_json_string(row["action"], "action")),
                   decode_json_object(row["arguments"]), values["owner_task_id"], values["resource_id"],
                   values["effect_ref"], deadline, require_json_string(row["target_id"], "target_id"),
                   require_json_string(row["frame_id"], "frame_id"))


@dataclass(frozen=True)
class BrowserConfig:
    browser_path: str = ""
    profile_dir: str = ""
    cdp_port: int = 0
    headless: bool = True
    launch: bool = True
    isolated: bool = True

    def payload(self) -> JsonObject:
        return {"browser_path": self.browser_path, "profile_dir": self.profile_dir, "cdp_port": self.cdp_port,
                "headless": self.headless, "launch": self.launch, "isolated": self.isolated}

    @classmethod
    def read(cls, raw: object) -> BrowserConfig:
        from codey.types.json_value import require_json_integer

        row = decode_json_object(raw)
        if set(row) != {"browser_path", "profile_dir", "cdp_port", "headless", "launch", "isolated"}:
            raise ValueError("invalid browser configuration")
        port = require_json_integer(row["cdp_port"], "cdp_port")
        if not 0 <= port < 65536:
            raise ValueError("invalid browser port")
        return cls(require_json_string(row["browser_path"], "browser_path"),
                   require_json_string(row["profile_dir"], "profile_dir"), port,
                   require_json_boolean(row["headless"], "headless"),
                   require_json_boolean(row["launch"], "launch"), require_json_boolean(row["isolated"], "isolated"))


class RouteRequest(Protocol):
    @property
    def url(self) -> str: ...


class RouteDecision(Protocol):
    @property
    def request(self) -> RouteRequest: ...

    def abort(self, error_code: str | None = None) -> None: ...

    def continue_(self) -> None: ...

    def fulfill(self, *, status: int = 200, content_type: str | None = None,
                headers: dict[str, str] | None = None, body: str | bytes | None = None) -> None: ...
