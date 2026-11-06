from tests.manual import research_semantic_review_d_analysis as analysis


def test_corrected_rubric_counts_candidate_claim_as_task_completion():
    row = {"case": "known_url", "answer": "3.22.0 -shm -wal writable immutable 候选 Claim [e1]"}
    score = analysis.corrected_score(row)
    assert score["required_hit"] == score["required_total"] == 6
    assert score["forbidden_count"] == 0


def test_corrected_rubric_does_not_flag_honest_speed_uncertainty():
    row = {"case": "error_complex", "answer": "\u8fd9\u4e9b\u653f\u7b56\u4e0d\u80fd\u8bc1\u660e\u672c\u673a\u901f\u5ea6\u66f4\u5feb\u3002"}
    score = analysis.corrected_score(row)
    assert score["forbidden_count"] == 0


def test_corrected_rubric_flags_key_alone_and_unknown_citation():
    row = {"case": "known_url", "answer": "key alone suffices [e9]"}
    score = analysis.corrected_score(row)
    assert score["forbidden_count"] == 1
    assert score["citation_labels_valid"] is False
