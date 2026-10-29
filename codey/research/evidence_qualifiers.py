"""Conservative explicit qualifier conflicts, not general entailment checking."""

from __future__ import annotations

import re

_NONSIGNIFICANCE = re.compile(
    r"\b(?:(?:not|no)\s+(?:statistically\s+)?significant|non[-\s]*significant)\b"
    r"|(?:不|未|无)(?:达到|达|具有|有|见)?(?:统计学|统计)?(?:上)?(?:显著|意义)",
    re.IGNORECASE,
)
_SIGNIFICANCE = re.compile(r"\bsignificant(?:ly)?\b|显著", re.IGNORECASE)


def support_qualifier_problem(claim: str, excerpt: str) -> str:
    """Flag an explicit lost nonsignificance qualifier, without choosing alpha.

    A quote containing several comparisons may report both significant and
    nonsignificant effects. Ask the writer to split or retain that distinction;
    do not declare all its findings false or infer significance from p-values.
    """
    if not _NONSIGNIFICANCE.search(excerpt) or _NONSIGNIFICANCE.search(claim):
        return ""
    for match in _SIGNIFICANCE.finditer(claim):
        surrounding = claim[max(0, match.start() - 48):match.end() + 24]
        # Uncertainty or an explicit denial is not an affirmative effect claim.
        if re.search(r"\b(?:not|no|unclear|unknown|uncertain)\b|不|未|无|未知|不明|尚待", surrounding, re.IGNORECASE):
            continue
        return ("the excerpt explicitly reports a nonsignificant comparison, but the claim asserts "
                "a significant effect without retaining that limit. Recheck which intervention, comparator "
                "and endpoint each result belongs to. Split different comparisons into separate evidence "
                "items with their own exact passages, or preserve the nonsignificance distinction in the claim. "
                "Do not transfer a significant result from another comparison. No evidence was saved. "
                "This qualifier conflict is not a general statistical or entailment review.")
    return ""
