"""The recovery oracle checks the requested artifact, not just test exit and hashes."""

import hashlib
import json

from tools.local_model_gate_attempts import GateTarget
from tools.local_model_gate_recovery import _verify_recovery, run_recovery_case


def test_consistent_receipt_hash_cannot_hide_wrong_checkpoint_content(tmp_path):
    target = GateTarget("http://localhost:5001/v1", "scripted", 32768, 8192, 12000, protocol="json")
    project = tmp_path / "project"
    (tmp_path / "input.json").write_text(json.dumps({"project": str(project), "state": str(tmp_path / "state")}))
    assert run_recovery_case(target, tmp_path, scripted=True)["ok"] is True
    checkpoint = project / "checkpoint.py"
    checkpoint.write_text("VALUE = 42\n# unrequested content\n")
    interruption_path = tmp_path / "interruption.json"
    interruption = json.loads(interruption_path.read_text())
    interruption["file_sha256"] = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    interruption_path.write_text(json.dumps(interruption))
    result = _verify_recovery(tmp_path, project, 75)
    assert result["verification"]["ok"] is True
    assert result["ok"] is False, "an unchanged artifact can still violate the original task"
