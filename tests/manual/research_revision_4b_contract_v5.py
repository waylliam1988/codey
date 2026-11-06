"""Contract helpers for the next 4B revision experiment.

This module is experiment-only.  It keeps the original answer, evidence
packets and citation labels intact and describes the required call graph for
the four arms before any real writer run is started.
"""
from __future__ import annotations

from dataclasses import dataclass

ARMS = ("U", "R", "S", "V")


@dataclass(frozen=True)
class EvidencePacket:
    label: str
    source_id: str
    revision: int
    start: int
    end: int
    excerpt: str


@dataclass(frozen=True)
class DraftFixture:
    task: str
    draft: str
    evidence: tuple[EvidencePacket, ...]


def validate_fixture(fixture: DraftFixture) -> None:
    if not fixture.task.strip() or not fixture.draft.strip():
        raise ValueError("4B fixture task and draft must be non-empty")
    labels = [packet.label for packet in fixture.evidence]
    if not labels or len(labels) != len(set(labels)):
        raise ValueError("4B fixture needs distinct exact evidence labels")
    if any(not packet.excerpt.strip() for packet in fixture.evidence):
        raise ValueError("4B fixture cannot use empty evidence excerpts")
    for packet in fixture.evidence:
        if packet.start < 0 or packet.end < packet.start:
            raise ValueError("4B evidence locator is invalid")
        if f"[{packet.label}]" not in fixture.draft:
            raise ValueError(f"draft lost evidence label [{packet.label}]")


def arm_call_plan(arm: str) -> tuple[str, ...]:
    if arm not in ARMS:
        raise ValueError(f"unknown 4B arm: {arm}")
    return {
        "U": ("submit",),
        "R": ("revise", "submit"),
        "S": ("self_review", "revise", "submit"),
        "V": ("evidence_review", "revise", "submit"),
    }[arm]


def review_prompt(fixture: DraftFixture, kind: str) -> str:
    if kind not in {"self_review", "evidence_review"}:
        raise ValueError("review kind must be self_review or evidence_review")
    validate_fixture(fixture)
    role = "作者自查" if kind == "self_review" else "独立证据审查"
    evidence = "\n\n".join(f"[{p.label}] {p.excerpt}" for p in fixture.evidence)
    return (
        f"你执行{role}。只检查原任务、原稿和准确证据，输出需要修复的具体问题；"
        "不要改写证据、不要新增材料、不要直接提交。\n\n"
        f"原任务：{fixture.task}\n\n原稿：{fixture.draft}\n\n证据：{evidence}"
    )


def revision_prompt(fixture: DraftFixture, feedback: str = "") -> str:
    validate_fixture(fixture)
    evidence = "\n\n".join(f"[{p.label}] {p.excerpt}" for p in fixture.evidence)
    return (
        "重读原任务、原稿和准确证据后修订一次。反馈只是待核对建议，"
        "只能采用被证据支持的修复；保留正确内容和原始引用标签，不得凭空补材料。\n\n"
        f"原任务：{fixture.task}\n\n原稿：{fixture.draft}\n\n证据：{evidence}"
        + (f"\n\n待核对反馈：{feedback}" if feedback else "")
    )
