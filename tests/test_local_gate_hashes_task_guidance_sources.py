"""Live-gate metadata fingerprints every owner of the prepared task prompt."""

import hashlib

from tools import local_model_release_gate as gate
from tools.local_model_gate_attempts import REPO_ROOT, GateTarget


def test_live_gate_metadata_hashes_actual_task_guidance_and_context_sources(monkeypatch):
    monkeypatch.setattr(gate, "_server_observations", lambda _url: {})
    metadata = gate._metadata(GateTarget("http://model.test/v1", "test", 32768, 8192, 12000))
    for path in (
        "codey/operations/task_guidance.py", "codey/research/completion_guidance.py",
        "codey/research/tool_contract.py", "codey/reviews/report_sections.py",
        "codey/operations/project_prompt_context.py", "codey/workspace/coding_context.py",
        "codey/operations/project_candidate_validation.py", "codey/agents/context_compaction.py",
        "codey/providers/compaction.py", "codey/providers/context_checkpoint.py",
        "codey/providers/context_ledger.py",
    ):
        assert metadata["production_hashes"].get(path) == hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest()
