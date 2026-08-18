"""Shared domain-routing hints for Research source connectors."""

from __future__ import annotations


MEDICAL_CONNECTOR_TERMS = frozenset({
    "biomedical",
    "cancer",
    "clinical",
    "disease",
    "drug",
    "gene",
    "genetic",
    "genetics",
    "genomic",
    "genomics",
    "life",
    "life_science",
    "medicine",
    "patient",
    "pharma",
    "protein",
    "pubmed",
    "therapy",
    "trial",
    "vaccine",
    "医学",
    "临床",
    "疾病",
    "基因",
    "蛋白",
    "疫苗",
    "药物",
    "生命科学",
    "肿瘤",
    "癌症",
})
ARXIV_CONNECTOR_TERMS = frozenset({
    "algorithm",
    "arxiv",
    "benchmark",
    "computer",
    "diffusion",
    "learning",
    "llm",
    "machine",
    "math",
    "model",
    "neural",
    "nlp",
    "paper",
    "physics",
    "preprint",
    "quantum",
    "rag",
    "retrieval",
    "robotics",
    "transformer",
    "论文",
    "预印本",
    "机器学习",
    "模型",
    "算法",
    "物理",
})
LOCAL_CONNECTOR_TERMS = frozenset({
    "csv",
    "dataset",
    "file",
    "json",
    "local",
    "spreadsheet",
    "table",
    "tsv",
    "本地",
    "文件",
    "表格",
    "数据",
})


def preferred_connector_ids(
    terms: object,
    *,
    available_ids: tuple[str, ...] = (),
) -> tuple[str, ...]:
    term_set = {str(item or "").casefold() for item in _iter_terms(terms)}
    available = set(available_ids)
    preferred: list[str] = []
    if "pubmed" in available and term_set & MEDICAL_CONNECTOR_TERMS:
        preferred.append("pubmed")
    if "arxiv" in available and term_set & ARXIV_CONNECTOR_TERMS:
        preferred.append("arxiv")
    if "local_file" in available and term_set & LOCAL_CONNECTOR_TERMS:
        preferred.append("local_file")
    if "csv_tsv" in available and {
        "csv",
        "dataset",
        "spreadsheet",
        "table",
        "tsv",
        "表格",
        "数据",
    } & term_set:
        preferred.append("csv_tsv")
    if "json_file" in available and "json" in term_set:
        preferred.append("json_file")
    return tuple(preferred)


def _iter_terms(value: object):
    if isinstance(value, str):
        yield from value.split()
        return
    try:
        yield from value  # type: ignore[misc]
    except TypeError:
        text = str(value or "")
        if text:
            yield text


__all__ = [
    "ARXIV_CONNECTOR_TERMS",
    "LOCAL_CONNECTOR_TERMS",
    "MEDICAL_CONNECTOR_TERMS",
    "preferred_connector_ids",
]
