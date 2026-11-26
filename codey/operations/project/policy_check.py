from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectPolicyCheck:
    denied: bool
    message: str = ""
    approval_required: bool = False
    reason_code: str = ""
