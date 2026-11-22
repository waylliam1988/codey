"""Compare what the report quotes with the exact passages its labels select."""
from __future__ import annotations

import re
from collections.abc import Mapping

from codey.research.answer_values import AnswerSubmission
from codey.research.evidence_gaps import same_source_groups
from codey.research.passages import ReadPassage
from codey.types.json_value import JsonObject, JsonValue
from codey.utils.citation_scanner import mask_markdown_code

_DIRECT_QUOTE = re.compile(r'"([^"\n]+)"|“([^”\n]+)”|「([^」\n]+)」|『([^』\n]+)』')
_CITATION_TAIL = re.compile(r"[\s*_，,。.：:;；()（）!?！？]*\Z")
_CITATION_PREFIX = re.compile(r"[ \t*_：:]*")
_QUANTITY = re.compile(r"(?<![\w.])([0-9]+(?:[.,][0-9]+)?)\s*(%|°[CF]|µg|μg|mg|kg|g|ml|mL|L|km|cm|mm|m|ms|s|h|克|公斤|小时|分钟|天|人)(?![A-Za-z])")
_WORD = re.compile(r"[^\W\d_]{4,}")
_COMMON_WORDS = frozenset({"about", "average", "according", "approximately", "between", "reported", "showed", "observed", "results", "study", "source", "that", "this", "with", "from", "were"})


def _passage_span(passage: ReadPassage, quote: str) -> tuple[int, int] | None:
    # Match token boundaries and preserve the original Unicode offsets and whitespace.
    pattern = r"\s+".join(re.escape(part) for part in quote.split())
    match = re.search(pattern, passage.excerpt) if pattern else None
    return (passage.start + match.start(), passage.start + match.end()) if match is not None else None


def _quantities(clause: str, cited: Mapping[str, ReadPassage]) -> list[JsonValue]:
    diagnostics: list[JsonValue] = []
    terms = _terms(clause)
    for match in _QUANTITY.finditer(clause):
        quantity = match[1], match[2].casefold()
        if any((raw[1], raw[2].casefold()) == quantity and (not terms or terms <= _terms(text))
               for passage in cited.values() for text in re.split(r"(?<=[.!?。！？])\s+|\n+", passage.excerpt)
               for raw in _QUANTITY.finditer(text)):
            continue
        diagnostics.append({"kind": "quantity_needs_context", "quantity": match[0], "labels": list(cited)})
    return diagnostics


def _terms(text: str) -> set[str]:
    return set(_WORD.findall(text.casefold())) - _COMMON_WORDS


def check_report_evidence(submission: AnswerSubmission, passages: Mapping[str, ReadPassage]) -> JsonObject:
    markdown = mask_markdown_code(submission["markdown"])
    browser = bool(submission["scope"].get("execution_refs"))
    validated: list[JsonValue] = []
    unverified: list[JsonValue] = []
    groups: dict[tuple[int, int], dict[str, ReadPassage]] = {}
    for row in submission["citations"]:
        passage = passages.get(row["passage_ref"])
        if passage is not None:
            groups.setdefault((row["start"], row["end"]), {})[row["label"]] = passage
    # Consecutive citations choose one evidence set for the immediately preceding clause.
    groups = _adjacent_groups(groups)
    clause_stop = 0
    for (start, stop), cited in groups.items():
        line_start = markdown.rfind("\n", 0, start) + 1
        clause = markdown[max(clause_stop, line_start):start]
        clause_stop = stop
        quotes = [quote for quote in _DIRECT_QUOTE.finditer(clause) if _CITATION_TAIL.fullmatch(clause[quote.end():])]
        prefix = _CITATION_PREFIX.match(markdown, stop)
        following = _DIRECT_QUOTE.match(markdown, prefix.end()) if prefix is not None else None
        if following is not None:
            quotes.append(following)
            clause_stop = following.end()
        for quote in quotes:
            value = next(part for part in quote.groups() if part is not None).strip()
            if not value or (not browser and len(value) < 12):
                continue
            located = next(((label, passage, span) for label, passage in cited.items()
                            if (span := _passage_span(passage, value)) is not None), None)
            if located is None:
                code = "browser_quote_not_in_source" if browser else "quote_not_in_source"
                raise ValueError(code + ": Cited quotation is absent from its bound passage (" + ", ".join(cited)
                                 + "). Use the exact text from the cited passage, or present a paraphrase without quotation marks.")
            label, passage, (source_start, source_end) = located
            validated.append({"label": label, "passage_ref": passage.ref, "start": source_start, "end": source_end})
        unverified.extend(_quantities(clause, cited))
    selected = tuple(dict.fromkeys((row["label"], row["passage_ref"]) for row in submission["citations"]))
    for label, ref in selected:
        passage = passages.get(ref)
        if passage is not None and (publication := passage.source.publication) is not None and publication.status:
            unverified.append({"kind": "publication_status", "label": label, "status": publication.status})
    groups_of_sources = same_source_groups(passages, selected)
    return {"verified_quotes": validated, "needs_review": unverified,
            "shared_source_groups": [list[JsonValue](labels) for labels in groups_of_sources]}


def _adjacent_groups(groups: dict[tuple[int, int], dict[str, ReadPassage]]) -> dict[tuple[int, int], dict[str, ReadPassage]]:
    result: dict[tuple[int, int], dict[str, ReadPassage]] = {}
    for (start, end), labels in groups.items():
        previous = next(reversed(result), None)
        if previous is not None and start == previous[1]:
            result[previous[0], end] = {**result.pop(previous), **labels}
        else:
            result[start, end] = labels
    return result
