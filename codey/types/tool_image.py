"""Immutable references to captured bytes, never live screenshot instructions."""
from __future__ import annotations

from dataclasses import dataclass

from codey.types.json_value import JsonObject, decode_json_object, require_json_integer, require_json_string

TOOL_IMAGES_KEY = "_codey_tool_images"


@dataclass(frozen=True)
class ToolImageRef:
    state_home: str
    session_id: str
    run_id: str
    handle: str
    sha256: str
    size: int
    width: int
    height: int
    observation_ref: str
    media_type: str = "image/png"

    def __post_init__(self) -> None:
        if (self.media_type != "image/png" or not 0 < self.size <= 8 * 1024 * 1024
                or not 0 < self.width * self.height <= 4_000_000 or min(self.width, self.height) <= 0
                or len(self.sha256) != 64 or any(char not in "0123456789abcdef" for char in self.sha256)
                or not all((self.state_home, self.session_id, self.run_id, self.handle, self.observation_ref))):
            raise ValueError("invalid captured tool image reference")

    def payload(self) -> JsonObject:
        return {"state_home": self.state_home, "session_id": self.session_id, "run_id": self.run_id,
                "handle": self.handle, "sha256": self.sha256, "size": self.size, "width": self.width,
                "height": self.height, "observation_ref": self.observation_ref, "media_type": self.media_type}

    @classmethod
    def read(cls, value: object) -> ToolImageRef:
        row = decode_json_object(value)
        if set(row) != {"state_home", "session_id", "run_id", "handle", "sha256", "size", "width", "height",
                        "observation_ref", "media_type"}:
            raise ValueError("invalid captured image reference fields")
        return cls(require_json_string(row["state_home"], "state_home"),
                   require_json_string(row["session_id"], "session_id"), require_json_string(row["run_id"], "run_id"),
                   require_json_string(row["handle"], "handle"), require_json_string(row["sha256"], "sha256"),
                   require_json_integer(row["size"], "size"), require_json_integer(row["width"], "width"),
                   require_json_integer(row["height"], "height"), require_json_string(row["observation_ref"], "observation_ref"),
                   require_json_string(row["media_type"], "media_type"))
