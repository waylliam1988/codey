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


def test_interrogative_topics_take_priority_over_following_delivery_instructions():
    terms = question_terms('研究 SQLite WAL：为什么读写可并发，而写者仍竞争？保存两条中文主张。'
                           '然后使用 knowledge_write，报告须使用 done.report_sections。')
    assert "sqlite" in terms and "wal" in terms
    assert not set(terms) & {"为什么读写可并发", "保存两条中文主张", "knowledge_write", "report_sections"}
    assert "lithium" in question_terms("What affects lithium supply? Save a report.")


def test_explicit_quoted_topics_are_kept_including_long_chinese_names():
    terms = question_terms('Compare "snapshot isolation" and "serializable isolation". 研究"肿瘤种群演化与选择压力"。')
    assert "snapshot isolation" in terms and "serializable isolation" in terms
    assert "肿瘤种群演化与选择压力" in terms


def test_post_question_comparisons_keep_versions_and_topics_but_not_storage_instructions():
    terms = question_terms("How does SQLite WAL work? Compare versions 3.11 and 3.12; "
                           "check checkpoint starvation. Save with knowledge_write. Return a 1000 word report.")
    assert {"sqlite", "wal", "3.11", "3.12", "checkpoint", "starvation"} <= set(terms)
    assert not {"knowledge_write", "1000", "report"} & set(terms)


def test_chinese_comparison_after_question_keeps_explicit_version_requirement():
    terms = question_terms("SQLite WAL 是什么？请实际打开官网，重点比较 3.11 与 3.12；"
                           "检查 SQLITE_BUSY。保存中文主张，然后使用 knowledge_write。")
    assert {"sqlite", "wal", "3.11", "3.12", "sqlite_busy"} <= set(terms)
    assert "knowledge_write" not in terms
