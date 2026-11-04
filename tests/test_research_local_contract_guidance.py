"""Domain-specific method hints remain scoped to relevant subject matter."""

from codey.research.methods import research_method_guidance


def test_domain_transfer_is_not_a_clinical_signal():
    assert "clinical:" not in research_method_guidance("如何继续转移数据？", evidence_only=True)
    assert "clinical:" in research_method_guidance("癌症转移患者的治疗研究", evidence_only=True)
