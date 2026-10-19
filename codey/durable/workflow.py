"""Small, explicit contracts for versioned deterministic task workflows."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from codey.durable.model import thaw_json

WorkflowMigrator = Callable[[Mapping[str, object]], Mapping[str, object]]


def workflow_digest(
    name: str,
    version: int,
    workflow_input: Mapping[str, object],
    checkpoint: Mapping[str, object],
) -> str:
    payload = {"name": name, "version": version, "input": thaw_json(workflow_input),
               "checkpoint": thaw_json(checkpoint)}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    version: int
    migrate: WorkflowMigrator | None = None
    replay_contract: str = "deterministic-v1"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("workflow name is required")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("workflow version must be a positive integer")
        if not self.replay_contract:
            raise ValueError("workflow replay contract is required")

    def migrate_checkpoint(self, checkpoint: Mapping[str, object], from_version: int) -> dict[str, object]:
        if from_version > self.version:
            raise ValueError("stored workflow version is newer than definition")
        if from_version == self.version:
            return dict(checkpoint)
        if self.migrate is None:
            raise ValueError("workflow migration is required")
        migrated = self.migrate(checkpoint)
        if not isinstance(migrated, Mapping):
            raise ValueError("workflow migration must return a mapping")
        return dict(migrated)


__all__ = ["WorkflowDefinition", "workflow_digest"]
