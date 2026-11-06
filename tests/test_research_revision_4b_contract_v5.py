from tests.manual.research_revision_4b_contract_v5 import (
    ARMS,
    DraftFixture,
    EvidencePacket,
    arm_call_plan,
    review_prompt,
    revision_prompt,
    validate_fixture,
)


def fixture():
    return DraftFixture(
        task="Compare V1 and V2 limits.",
        draft="V1 is limited [e1], while V2 differs [e2].",
        evidence=(
            EvidencePacket("e1", "source-v1", 1, 0, 10, "V1 limit"),
            EvidencePacket("e2", "source-v2", 1, 20, 30, "V2 limit"),
        ),
    )


def test_fixture_preserves_distinct_exact_evidence_labels():
    value = fixture()
    validate_fixture(value)
    text = revision_prompt(value)
    assert "[e1] V1 limit" in text
    assert "[e2] V2 limit" in text
    assert "[e1]" in value.draft and "[e2]" in value.draft


def test_four_arms_have_distinct_review_call_graphs():
    assert ARMS == ("U", "R", "S", "V")
    assert arm_call_plan("U") == ("submit",)
    assert arm_call_plan("R") == ("revise", "submit")
    assert arm_call_plan("S") == ("self_review", "revise", "submit")
    assert arm_call_plan("V") == ("evidence_review", "revise", "submit")


def test_review_prompt_does_not_offer_submission_or_editable_evidence():
    text = review_prompt(fixture(), "evidence_review")
    assert "不要直接提交" in text
    assert "不要改写证据" in text
