"""One ephemeral evaluation shared by completion proof and repair feedback."""
from dataclasses import dataclass


@dataclass(frozen=True)
class RequirementEvaluation:
    reason_code: str = ""
    feedback: str = ""

    @property
    def passed(self) -> bool:
        return not self.reason_code
