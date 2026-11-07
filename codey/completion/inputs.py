"""Detached local completion evidence consumed independently of trace layout."""

from __future__ import annotations

from dataclasses import dataclass

from codey.types.json_value import (
    JsonObject,
    canonical_json_object,
    require_json_boolean,
    require_json_integer,
    require_json_string,
)


@dataclass(frozen=True)
class ChangedFile:
    path: str
    previous_path: str = ""
    status: str = ""
    additions: int | None = None
    deletions: int | None = None

    def to_payload(self) -> JsonObject:
        row: JsonObject = {"path": self.path, "previous_path": self.previous_path, "status": self.status}
        if self.additions is not None:
            row["additions"] = self.additions
        if self.deletions is not None:
            row["deletions"] = self.deletions
        return row


@dataclass(frozen=True)
class CompletionChangeSet:
    files: tuple[ChangedFile, ...] = ()
    diff: str = ""
    truncated: bool = False
    ok: bool = True
    changed_count: int = 0
    mode: str = ""

    def to_payload(self) -> JsonObject:
        return {"files": [item.to_payload() for item in self.files], "diff": self.diff,
                "truncated": self.truncated, "ok": self.ok,
                "changed_count": self.changed_count, "mode": self.mode}

    @classmethod
    def from_value(cls, value: object) -> CompletionChangeSet:
        if isinstance(value, cls):
            return value
        row = canonical_json_object(value)
        raw_files = row.get("files", [])
        if not isinstance(raw_files, list):
            raise ValueError("completion changes.files must be a list")
        files: list[ChangedFile] = []
        for item in raw_files:
            if not isinstance(item, dict):
                raise ValueError("completion change file must be an object")
            files.append(ChangedFile(
                require_json_string(item.get("path", ""), "change path"),
                require_json_string(item.get("previous_path", ""), "previous_path"),
                require_json_string(item.get("status", ""), "change status"),
                None if item.get("additions") is None else require_json_integer(item["additions"], "additions"),
                None if item.get("deletions") is None else require_json_integer(item["deletions"], "deletions"),
            ))
        return cls(tuple(files), require_json_string(row.get("diff", ""), "diff"),
            require_json_boolean(row.get("truncated", False), "truncated"),
            require_json_boolean(row.get("ok", True), "changes.ok"),
            require_json_integer(row.get("changed_count", len(files)), "changed_count"),
            require_json_string(row.get("mode", ""), "changes.mode"))


@dataclass(frozen=True)
class AnalysisRunEvidence:
    """The exact trace fields needed for matching decisive local executions."""

    analysis_run_id: str
    command_display: str
    cwd_digest: str

    def to_payload(self) -> JsonObject:
        return {"analysis_run_id": self.analysis_run_id, "command_display": self.command_display,
                "cwd_ref": {"digest": self.cwd_digest}}

    @classmethod
    def from_value(cls, value: object) -> AnalysisRunEvidence:
        if isinstance(value, cls):
            return value
        row = canonical_json_object(value)
        cwd = row.get("cwd_ref", {})
        if not isinstance(cwd, dict):
            raise ValueError("analysis run cwd_ref must be an object")
        return cls(require_json_string(row.get("analysis_run_id", ""), "analysis_run_id"),
            require_json_string(row.get("command_display", ""), "command_display"),
            require_json_string(cwd.get("digest", ""), "cwd digest"))
