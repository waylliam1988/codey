"""Conservative explicit qualifier conflicts, not general entailment checking."""

from __future__ import annotations

import re

_NONSIGNIFICANCE = re.compile(
    r"\b(?:(?:not|no)\s+(?:statistically\s+)?significant|non[-\s]*significant)\b"
    r"|(?:不|未|无)(?:达到|达|具有|有|见)?(?:统计学|统计)?(?:上)?(?:显著|意义)",
    re.IGNORECASE,
)
_SIGNIFICANCE = re.compile(r"\bsignificant(?:ly)?\b|显著", re.IGNORECASE)
_MODAL_EFFECT = re.compile(
    r"\b(?:may|might)\s+(?:also\s+)?(?:increase|decrease|improve|reduce|slow|stop|prevent|delay|"
    r"prolong|enhance|suppress|inhibit|help)\b", re.IGNORECASE,
)
_RETAINED_UNCERTAINTY = re.compile(
    r"\b(?:may|might|hypothes\w*|aim\w*|investigat\w*|evaluat\w*|uncertain|unproven)\b"
    r"|可能|假设|旨在|有望|尚未|尚待|未证实|不确定|评估|拟验证", re.IGNORECASE,
)


def support_qualifier_problem(claim: str, excerpt: str) -> str:
    """Flag narrow explicit qualifier losses, without general entailment claims.

    A quote containing several comparisons may report both significant and
    nonsignificant effects. Ask the writer to split or retain that distinction;
    do not declare all its findings false or infer significance from p-values.
    """
    if _MODAL_EFFECT.search(excerpt) and not _RETAINED_UNCERTAINTY.search(claim):
        return ("the excerpt says an effect may/might occur, but the supporting claim drops that uncertainty. "
                "Preserve the hypothesis or study aim in the claim; a proposed benefit is not a reported result. "
                "If another passage reports an actual outcome, save that separate exact passage instead. "
                "No evidence was saved. This narrow qualifier check is not a general entailment review.")
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
