"""Production publication follows the original writer result."""

from pathlib import Path

import pytest

from tests.test_research_whole_kernel import run_whole


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_publication_uses_knowledge_and_writes_no_evidence_json(tmp_path: Path, native: bool) -> None:
    result = run_whole(tmp_path, native=native)
    assert result.stop_reason == "done", result.summary
    assert result.synthesis_id and result.accepted_report is not None
    assert not tuple(tmp_path.rglob("evidence_ledgers"))
