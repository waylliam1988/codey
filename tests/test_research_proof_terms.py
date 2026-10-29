from codey.research.proof_terms import question_terms


def test_question_terms_do_not_treat_structured_task_examples_as_topics():
    terms = question_terms(
        'Research SQLite WAL; aliases=["embedded data file"], tags=["serial writes"], '
        'conditions={"mode":{"state":"reported","value":"WAL"},'
        '"workload":{"state":"not_extracted"}}. Use done.report_sections. '
        '```json\n{"claim":"ignored schema example"}\n```'
    )
    assert "sqlite" in terms and "wal" in terms
    assert not set(terms) & {"mode", "state", "reported", "workload", "not_extracted", "done", "report_sections"}
    assert "embedded data file" not in terms and "ignored schema example" not in terms


def test_long_chinese_clauses_are_not_verbatim_coverage_requirements():
    terms = question_terms("SQLite WAL 为什么读写能够并发而多个写入者却不能同时提交？checkpoint 有什么限制？")
    assert "sqlite" in terms and "wal" in terms and "checkpoint" in terms
    assert "为什么读写能够并发而多个写入者却不能同时提交" not in terms
    assert "有什么限制" in terms


def test_explicit_quoted_topics_are_kept_including_long_chinese_names():
    terms = question_terms('Compare "snapshot isolation" and "serializable isolation". 研究"肿瘤种群演化与选择压力"。')
    assert "snapshot isolation" in terms and "serializable isolation" in terms
    assert "肿瘤种群演化与选择压力" in terms
