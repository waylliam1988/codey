"""Exact immutable binding in the research receipt projection."""

import re
from dataclasses import asdict, dataclass
from typing import Any

from codey.knowledge.model import KnowledgeReference


@dataclass(frozen=True)
class ResearchReference:
    object_id: str
    revision: int
    kind: str
    acquisition: str
    note_id: str = ""
    passage_ref: str = ""
    question_ref: str = ""

    def __post_init__(self) -> None:
        KnowledgeReference(self.object_id, self.revision, "uses")
        if self.kind not in {"claim", "inference", "observation"} or self.acquisition not in {"current_run", "prior_knowledge"}:
            raise ValueError("invalid research reference kind or acquisition")
        if not isinstance(self.note_id, str) or not isinstance(self.passage_ref, str):
            raise ValueError("invalid research reference locator")
        if not isinstance(self.question_ref, str) or self.question_ref and not re.fullmatch(r"q[1-8]", self.question_ref):
            raise ValueError("invalid research reference question")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
