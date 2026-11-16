"""The live gate accepts declared status paraphrases while preserving exact times."""
from __future__ import annotations

import pytest

from tools.research_query_delivery_live_gate import fact_covered


@pytest.mark.parametrize('answer', ['状态：运输中', '包裹仍在途', 'Status: In transit', '包裹运送中'])
def test_live_status_accepts_equivalent_terms(answer: str) -> None:
    assert fact_covered(answer, 'In transit')


def test_live_gate_keeps_the_actual_arrival_value() -> None:
    assert fact_covered('预计周五 18:37 送达', '18:37')
    assert not fact_covered('预计 10:30 AM 送达', '18:37')
    assert not fact_covered('已经送达', 'In transit')
