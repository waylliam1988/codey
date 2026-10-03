"""Internal consumers use the owning module instead of obsolete facades."""

from codey.operations import task_phases, task_run
from codey.research import proof_quality, source_trust


def test_task_lifecycle_does_not_reexport_provider_health_helpers():
    for module in (task_run, task_phases):
        for name in ("record_provider_failure_event", "record_provider_success_event"):
            assert not hasattr(module, name)


def test_proof_review_uses_source_trust_owner_directly():
    assert not hasattr(proof_quality, "_source_trust_warnings")
    assert proof_quality.source_trust_warnings is source_trust.source_trust_warnings
