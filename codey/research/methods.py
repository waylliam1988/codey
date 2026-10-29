"""Composable research methods: guidance data, without executors or state."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResearchMethod:
    name: str
    terms: tuple[str, ...]
    conditions: tuple[str, ...]
    sources: str
    evaluation: str


METHODS = (
    ResearchMethod("software", ("code", "database", "sqlite", "软件", "数据库", "算法", "性能"),
                   ("version", "environment", "workload", "reproduction"),
                   "source code, official versioned documentation, reproducible tests and benchmarks",
                   "separate documented guarantees, observed behavior and hypotheses; check workload and version"),
    ResearchMethod("empirical", ("study", "experiment", "研究", "实验", "材料", "心理"),
                   ("population_or_model", "design", "comparator", "endpoint", "measurement"),
                   "original studies, datasets, systematic reviews and replication attempts",
                   "compare design, bias, independent datasets, uncertainty and contrary findings"),
    ResearchMethod("clinical", ("treatment", "cancer", "患者", "治疗", "癌", "用药", "转移"),
                   ("population", "disease_or_subtype", "intervention", "comparator", "endpoint", "study_design"),
                   "clinical trials, trial registries, current guidelines, systematic reviews; mechanisms as background",
                   "retain in vitro/animal/human distinctions and applicability; never infer individual treatment "
                   "instructions from mechanisms or graph paths"),
    ResearchMethod("causal", ("cause", "effect", "导致", "因果", "为什么", "影响"),
                   ("exposure", "outcome", "confounding", "alternative_explanations", "time_order"),
                   "controlled comparisons, natural experiments and competing causal explanations",
                   "distinguish association from intervention effects and check plausible confounders"),
    ResearchMethod("historical", ("history", "historical", "历史", "档案", "史料"),
                   ("period", "location", "authorship", "context"),
                   "primary archives, contemporaneous accounts, critical editions and independent scholarship",
                   "check provenance, dating, source dependence and competing interpretations"),
    ResearchMethod("business", ("market", "business", "商业", "市场", "投资", "财报"),
                   ("market", "period", "sample", "assumptions", "decision_threshold"),
                   "filings, primary market data, customer observations and explicit forecasts",
                   "separate measured data, forecasts and decisions; test sensitivity to assumptions"),
    ResearchMethod("mathematical", ("proof", "theorem", "证明", "定理", "数学"),
                   ("definitions", "assumptions", "domain"),
                   "precise definitions, proofs, counterexamples and independently checkable derivations",
                   "empirical agreement is not proof; inspect assumptions and boundary cases"),
)


def research_method_guidance(question: str) -> str:
    text = question.casefold()
    selected = [method for method in METHODS if any(term in text for term in method.terms)]
    lines = [
        "Research method:",
        "Clarify the question, constraints and what evidence would change the answer. Search saved knowledge, "
        "then investigate direct evidence and competing explanations. Use additional mechanism/constraint queries "
        "to discover relevant knowledge even when exact terms differ. A new connection remains a candidate until checked.",
        "Preserve observed results, author interpretations, user statements and model inferences separately. "
        "For knowledge_write, conditions can use {name:{state:reported,value:...}} or state "
        "not_reported/not_extracted/not_applicable. Leave unknown information unknown; never fill a template by guessing.",
        "Before done, check important claims against original evidence, contrary findings, scope and time. "
        "Record unresolved questions and limitations; search absence is not a negative finding. "
        "Multiple reports of one study are not independent evidence. Old answers, Ghost affinity and "
        "graph proximity cannot increase evidence strength. Read current versions before revising knowledge.",
    ]
    for method in selected[:4]:
        lines.append(f"{method.name}: sources: {method.sources}. Extract when available: "
                     f"{', '.join(method.conditions)}. Check: {method.evaluation}.")
    lines.append("Combine relevant methods; these suggestions are not an exhaustive domain classification.")
    return "\n".join(lines)
