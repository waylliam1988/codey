"""Compare what the report quotes with the exact passages its labels select."""
from __future__ import annotations

import re
from collections.abc import Collection, Iterator, Mapping
from decimal import Decimal

from codey.research.answer_contract import AnswerScope, digest, submit_answer
from codey.research.answer_values import AnswerSubmission, evidence_status_suffix
from codey.research.evidence_gaps import same_source_groups
from codey.research.passages import ReadPassage
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from codey.utils.citation_scanner import mask_markdown_code

_DIRECT_QUOTE = re.compile(r'"([^"\n]+)"|“([^”\n]+)”|「([^」\n]+)」|『([^』\n]+)』')
_CITATION_TAIL = re.compile(r"[\s*_，,。.：:;；()（）!?！？]*\Z")
_CITATION_PREFIX = re.compile(r"[ \t*_：:]*")
_NUMBER = r"[0-9]+(?:[.,][0-9]+)?"
_UNITS = r"%|°[CF]|µg|μg|mg|kg|g|ml|mL|L|km|cm|mm|m|ms|s|h|克|公斤|小时|分钟|天|人"
_QUANTITY = re.compile(rf"(?<![\w.])([+-]?{_NUMBER})\s*({_UNITS})(?![A-Za-z])")
_LEGACY_QUANTITY = re.compile(rf"(?<![\w.])({_NUMBER})\s*({_UNITS})(?![A-Za-z])")
_SUM_OR_DIFFERENCE = re.compile(r"\(\s*(?P<left>[^()\n]{1,32}?)\s*(?P<operator>[+−-])\s*(?P<right>[^()\n]{1,32})\s*\)")
_WORD = re.compile(r"[^\W\d_]{4,}")
_COMMON_WORDS = frozenset({"about", "average", "according", "approximately", "between", "reported", "showed", "observed", "results", "study", "source", "that", "this", "with", "from", "were"})
CLAIM_CHECK_VERSION = 3
CLAIM_CHECK_VERSIONS = frozenset({1, 2, CLAIM_CHECK_VERSION})
_CLAIM_BREAK = re.compile(r"(?<=[.!?])\s+|[;；。！？\n]+|(?:,\s*)?\b(?:but|whereas|however)\b|，(?:但|然而)", re.I)
_CLAIM_LEADING = re.compile(r"\s*(?:[-*+] |\d+[.)] )?[*_]*")
_GROUP_LABEL = re.compile(r"\b(group|arm|cohort)\s+([a-z0-9]+)\b|(?<![a-z0-9])([a-z0-9]+)\s*[组組]", re.I)
_RESPONSE_RATE = re.compile(r"(?:\bresponse\s+rate\b\s*(?:(?:was|is|of)\s+)?|(?:响应|響應|应答|應答)率\s*(?:为|為)?\s*)", re.I)
_EQUIVALENT_UNITS = {"克": "g", "公斤": "kg", "小时": "h", "μg": "µg"}


def _passage_span(passage: ReadPassage, quote: str) -> tuple[int, int] | None:
    # Match token boundaries and preserve the original Unicode offsets and whitespace.
    pattern = r"\s+".join(re.escape(part) for part in quote.split())
    match = re.search(pattern, passage.excerpt) if pattern else None
    return (passage.start + match.start(), passage.start + match.end()) if match is not None else None


def _quantities(clause: str, cited: Mapping[str, ReadPassage], verified: set[int], *, legacy: bool) -> list[JsonValue]:
    diagnostics: list[JsonValue] = []
    terms = _terms(clause)
    # Historical submissions without calculation checks retain their original
    # diagnostics when the archive verifies the immutable submission receipt.
    pattern = _LEGACY_QUANTITY if legacy else _QUANTITY
    for match in pattern.finditer(clause):
        if match.start() in verified:
            continue
        quantity = match[1], match[2].casefold()
        if any((raw[1], raw[2].casefold()) == quantity and (not terms or terms <= _terms(text))
               for passage in cited.values() for text in re.split(r"(?<=[.!?。！？])\s+|\n+", passage.excerpt)
               for raw in pattern.finditer(text)):
            continue
        diagnostics.append({"kind": "quantity_needs_context", "quantity": match[0], "labels": list(cited)})
    return diagnostics


def _terms(text: str) -> set[str]:
    return set(_WORD.findall(text.casefold())) - _COMMON_WORDS


def _calculation_inputs(cited: Mapping[str, ReadPassage], operand: re.Match[str]) -> list[JsonValue]:
    return [{"label": label, "passage_ref": passage.ref, "start": passage.start + found.start(),
             "end": passage.start + found.end()} for label, passage in cited.items()
            for found in _QUANTITY.finditer(passage.excerpt) if found.groups() == operand.groups()]


def _claim_row(start: int, end: int, kind: str, state: str, *, refs: tuple[str, ...] = (),
               spans: list[JsonValue] | None = None) -> JsonObject:
    return {"report_start": start, "report_end": end, "passage_refs": list(refs), "kind": kind, "review_state": state,
            **({"source_spans": spans} if spans else {})}


def _calculations(clause: str, cited: Mapping[str, ReadPassage], findings: list[JsonValue],
                  *, offset: int = 0, checks: list[JsonValue] | None = None) -> set[int]:
    verified: set[int] = set()
    for expression in _SUM_OR_DIFFERENCE.finditer(clause):
        result = next(reversed(tuple(_QUANTITY.finditer(clause, 0, expression.start()))), None)
        left = _QUANTITY.fullmatch(expression["left"].strip())
        right = _QUANTITY.fullmatch(expression["right"].strip())
        if (result is None or left is None or right is None or result[2] != left[2] or left[2] != right[2]
                or clause[result.end():expression.start()].strip(" \t*_")):
            continue
        numerals = [row[1] for row in (result, left, right)]
        if any("," in numeral or len(numeral) > 12 for numeral in numerals):
            continue
        expected, first, second = map(Decimal, numerals)
        inputs_left, inputs_right = _calculation_inputs(cited, left), _calculation_inputs(cited, right)
        if not inputs_left or not inputs_right:
            continue
        addition = expression["operator"] == "+"
        actual = first + second if addition else first - second
        if actual != expected:
            raise ValueError(f"calculation_mismatch: {left[1]} {expression['operator']} {right[1]} = {actual} {result[2]}; correct the expression or reported value")
        findings.append({"operation": "add" if addition else "subtract", "result": str(actual), "unit": result[2],
                         "left": left[1], "right": right[1], "left_sources": inputs_left, "right_sources": inputs_right})
        if checks is not None:
            spans = [*inputs_left, *inputs_right]
            refs = tuple(dict.fromkeys(ref for span in spans if isinstance(span, dict)
                                       and isinstance(ref := span.get("passage_ref"), str)))
            checks.append(_claim_row(offset + result.start(), offset + expression.end(), "calculation", "verified",
                                     refs=refs, spans=spans))
        # The arithmetic is proven; matching numbers alone does not establish
        # the source's entity, denominator, time or study conditions.
        verified.add(result.start())
    return verified


def _claim_parts(markdown: str, start: int, stop: int) -> Iterator[tuple[int, str]]:
    ends = [(match.start(), match.end()) for match in _CLAIM_BREAK.finditer(markdown, start, stop)]
    for end, resume in [*ends, (stop, stop)]:
        left = _CLAIM_LEADING.match(markdown, start, end)
        if left is not None:
            start = left.end()
        body = markdown[start:end].rstrip(" \t\r*_.,!?：:")
        if body and not body.startswith("#"):
            yield start, body
        start = resume


def _claim_segments(markdown: str, start: int, stop: int, cited: Mapping[str, ReadPassage],
                    version: int) -> list[JsonValue]:
    segments: list[JsonValue] = []
    for offset, text in _claim_parts(markdown, start, stop):
        if (_QUANTITY.search(text) or len(_terms(text)) >= 3 or len(text) >= 30
                or version >= 2 and _RESPONSE_RATE.search(text)):
            segments.append(_claim_segment(text, offset, cited))
    return segments


def _claim_segment(body: str, start: int, cited: Mapping[str, ReadPassage]) -> JsonObject:
    refs = tuple(dict.fromkeys(passage.ref for passage in cited.values()))
    normalized = " ".join(body.split())
    match = next(((passage, offset, text) for passage in cited.values() for excerpt in (passage.excerpt,)
                  for offset, text in _claim_parts(excerpt, 0, len(excerpt))
                  if " ".join(text.split()) == normalized), None)
    end = start + len(body)
    if match is None:
        return _claim_row(start, end, "claim", "needs_review" if refs else "uncited", refs=refs)
    passage, offset, text = match
    proof: JsonObject = {"passage_ref": passage.ref, "start": passage.start + offset, "end": passage.start + offset + len(text)}
    if passage.source_version:
        proof["source_version"] = list(passage.source_version)
    return _claim_row(start, end, "claim", "source_text_match", refs=(passage.ref,), spans=[proof])


def check_report_evidence(submission: AnswerSubmission, passages: Mapping[str, ReadPassage], *,
                          verify_calculations: bool = True, claim_check_version: int | None = None) -> JsonObject:
    if claim_check_version is not None and (type(claim_check_version) is not int or claim_check_version not in CLAIM_CHECK_VERSIONS):
        raise ValueError("unsupported research claim check version")
    version = claim_check_version or 0
    status = evidence_status_suffix(submission["scope"]["schema_version"], submission["state"])
    markdown = mask_markdown_code(submission["markdown"].removesuffix(status))
    browser = bool(submission["scope"].get("execution_refs"))
    validated: list[JsonValue] = []
    unverified: list[JsonValue] = []
    calculations: list[JsonValue] = []
    claim_checks: list[JsonValue] | None = [] if claim_check_version is not None else None
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
        clause_start = max(clause_stop, line_start)
        if claim_checks is not None:
            claim_checks.extend(_claim_segments(markdown, clause_stop, clause_start, {}, version))
        clause = markdown[clause_start:start]
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
            proof: JsonObject = {"label": label, "passage_ref": passage.ref, "start": source_start, "end": source_end}
            validated.append(proof)
            if claim_checks is not None:
                offset = 0 if quote is following else clause_start
                claim_checks.append(_claim_row(offset + quote.start(), offset + quote.end(), "quote", "verified",
                                               refs=(passage.ref,), spans=[proof]))
        verified = (_calculations(clause, cited, calculations, offset=clause_start, checks=claim_checks)
                    if verify_calculations else set())
        unverified.extend(_quantities(clause, cited, verified, legacy=not verify_calculations))
        if claim_checks is not None:
            claim_checks.extend(_claim_segments(markdown, clause_start, start, cited, version))
    if claim_checks is not None:
        claim_checks.extend(_claim_segments(markdown, clause_stop, len(markdown), {}, version))
    selected = tuple(dict.fromkeys((row["label"], row["passage_ref"]) for row in submission["citations"]))
    for label, ref in selected:
        passage = passages.get(ref)
        if passage is not None and (publication := passage.source.publication) is not None and publication.status:
            unverified.append({"kind": "publication_status", "label": label, "status": publication.status})
    groups_of_sources = same_source_groups(passages, selected)
    return {"verified_quotes": validated, "needs_review": unverified,
            "shared_source_groups": [list[JsonValue](labels) for labels in groups_of_sources],
            **({"verified_calculations": calculations} if verify_calculations else {}),
            **({"claim_check_version": claim_check_version, "claim_checks": claim_checks} if claim_checks is not None else {})}


def _adjacent_groups(groups: dict[tuple[int, int], dict[str, ReadPassage]]) -> dict[tuple[int, int], dict[str, ReadPassage]]:
    result: dict[tuple[int, int], dict[str, ReadPassage]] = {}
    for (start, end), labels in groups.items():
        previous = next(reversed(result), None)
        if previous is not None and start == previous[1]:
            result[previous[0], end] = {**result.pop(previous), **labels}
        else:
            result[start, end] = labels
    return result


def review_submission_issues(submission: AnswerSubmission, evidence_check: JsonObject,
                             passages: Mapping[str, ReadPassage]) -> list[JsonObject]:
    """Identify incompatible details in closely aligned quantitative statements."""
    checks = evidence_check.get("claim_checks")
    if not isinstance(checks, list):
        return []
    markdown = submission["markdown"]
    version = evidence_check.get("claim_check_version")
    normalized_support = isinstance(version, int) and version >= 3
    labels = {row["passage_ref"]: row["label"] for row in submission["citations"]}
    issues: list[JsonObject] = []
    for raw in checks:
        row = decode_json_object(raw)
        start, stop, refs = row.get("report_start"), row.get("report_end"), row.get("passage_refs")
        if (row.get("review_state") != "needs_review" or not isinstance(start, int)
                or not isinstance(stop, int) or not isinstance(refs, list)):
            continue
        claim = markdown[start:stop]
        normalized = " ".join(claim.split())
        if len(normalized) > 500 or not _QUANTITY.search(_RESPONSE_RATE.sub(" response rate ", normalized)):
            continue
        originals = [(ref, body, " ".join(body.split()))
                     for ref in refs if isinstance(ref, str) and (passage := passages.get(ref)) is not None
                     for _, body in _claim_parts(passage.excerpt, 0, len(passage.excerpt))]
        if any(original.casefold() == normalized.casefold() or
               normalized_support and _quantity_supports(normalized, original) for _, _, original in originals):
            continue
        for ref, body, original in originals:
            kind = _quantity_conflict(normalized, original)
            if kind:
                issues.append({"kind": kind, "report_start": start, "report_end": stop, "claim": claim,
                               "passage_ref": ref, "label": labels.get(ref, ref), "original": body})
                break
    return issues


def _quantity_conflict(claim: str, original: str) -> str:
    reported = tuple(_QUANTITY.finditer(_RESPONSE_RATE.sub(" response rate ", claim)))
    sourced = tuple(_QUANTITY.finditer(_RESPONSE_RATE.sub(" response rate ", original)))
    if len(reported) != 1 or len(sourced) != 1:
        return ""
    left, right = reported[0], sourced[0]
    left_unit = _EQUIVALENT_UNITS.get(left[2], left[2])
    right_unit = _EQUIVALENT_UNITS.get(right[2], right[2])
    # Commas are ambiguous across locales. Leave their interpretation to the writer.
    if "," in left[1] or "," in right[1]:
        return ""
    if (Decimal(left[1]), left_unit) != (Decimal(right[1]), right_unit):
        if _QUANTITY.sub("<quantity>", claim).casefold() == _QUANTITY.sub("<quantity>", original).casefold():
            return "quantity_conflict"
    else:
        subject, source_subject = _GROUP_LABEL.search(claim), _GROUP_LABEL.search(original)
        if (subject is not None and source_subject is not None
                and (subject[2] or subject[3]).casefold() != (source_subject[2] or source_subject[3]).casefold()
                and _group_claim_template(claim) == _group_claim_template(original)):
            return "group_conflict"
    return ""


def _quantity_supports(claim: str, original: str) -> bool:
    basis = _normalized_quantity_claim(claim)
    return basis is not None and basis == _normalized_quantity_claim(original)


def _normalized_quantity_claim(text: str) -> tuple[str, Decimal, str] | None:
    normalized = _RESPONSE_RATE.sub(" <response_rate> ", text)
    matches = tuple(_QUANTITY.finditer(normalized))
    if len(matches) != 1 or "," in matches[0][1]:
        return None
    quantity = matches[0]

    def group(match: re.Match[str]) -> str:
        return f"<{match[1] or 'group'}:{match[2] or match[3]}>"

    template = _GROUP_LABEL.sub(group, _QUANTITY.sub("<quantity>", normalized))
    return (" ".join(template.split()).replace("> <", "><").casefold(),
            Decimal(quantity[1]), _EQUIVALENT_UNITS.get(quantity[2], quantity[2]))


def _group_claim_template(text: str) -> str:
    def placeholder(match: re.Match[str]) -> str:
        return "<" + (match[1] or "group").casefold() + ">"

    masked = _GROUP_LABEL.sub(placeholder, text)
    # Align an explicitly recognized metric while keeping all other wording.
    return re.sub(r"\s+", "", _RESPONSE_RATE.sub("<response_rate>", masked)).casefold()


def research_review_key(issue: JsonObject) -> str:
    return digest([" ".join(str(issue["claim"]).split()).casefold(), issue["passage_ref"]])


def exhausted_review_answer(scope: AnswerScope, issue: JsonObject,
                            prior_keys: Collection[object]) -> AnswerSubmission | None:
    key = research_review_key(issue)
    if sum(item == key for item in prior_keys) < 2:
        return None
    label = issue["label"]
    citation = f" [{label}]" if any(alias == label for alias, _ in scope.aliases) else ""
    fields = ["{{field:" + field.pointer + "}}" for field in scope.fields]
    gap = "The requested conclusion could not be established. The repeated draft conflicts with the cited source" + citation + "."
    return submit_answer(scope, {"markdown": "\n\n".join((*fields, gap)),
        "state": "partial" if any(field.value is not None for field in scope.fields) else "unresolved"})
