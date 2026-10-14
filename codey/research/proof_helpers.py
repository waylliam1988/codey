from __future__ import annotations

from collections.abc import Mapping

from codey.research.object_model import ResearchRecord
from codey.utils.refs import identifier


def _record_payload(record: object) -> dict[str, object]:
    if record is None:
        return {}
    if isinstance(record, ResearchRecord):
        try:
            return dict(record.to_jsonable())
        except Exception:
            return {}
    if isinstance(record, Mapping):
        return dict(record)
    return {}

def _question_text(payload: Mapping[str, object]) -> str:
    question = payload.get('question')
    if not isinstance(question, Mapping):
        return ''
    return ''

def _source_map(value: object) -> dict[str, Mapping[str, object]]:
    rows: dict[str, Mapping[str, object]] = {}
    for item in _list_of_mappings(value):
        source_id = identifier(item.get('source_id'), 80)
        if source_id:
            rows[source_id] = item
    return rows

def _evidence_map(value: object) -> dict[str, Mapping[str, object]]:
    rows: dict[str, Mapping[str, object]] = {}
    for item in _list_of_mappings(value):
        evidence_id = identifier(item.get('evidence_id'), 80)
        if evidence_id:
            rows[evidence_id] = item
    return rows

def _claim_map(value: object) -> dict[str, Mapping[str, object]]:
    rows: dict[str, Mapping[str, object]] = {}
    for item in _list_of_mappings(value):
        claim_id = identifier(item.get('claim_id'), 80)
        if claim_id:
            rows[claim_id] = item
    return rows

def _assumption_map(value: object) -> dict[str, Mapping[str, object]]:
    rows: dict[str, Mapping[str, object]] = {}
    for item in _list_of_mappings(value):
        assumption_id = identifier(item.get('assumption_id'), 80)
        if assumption_id:
            rows[assumption_id] = item
    return rows

def _relation_list(value: object) -> tuple[Mapping[str, object], ...]:
    return tuple(_list_of_mappings(value))

def _list_of_mappings(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]

def _sequence_values(value: object) -> tuple[object, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()

def _positive_ints(value: object) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[int] = []
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, str) and (not item.strip().isascii()):
            continue
        try:
            number = int(item)
        except (TypeError, ValueError, OverflowError):
            continue
        if number > 0 and number not in out:
            out.append(number)
    return tuple(out)
