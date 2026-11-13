"""Pure selection and validation for a replaceable, protocol-valid context view."""
from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from dataclasses import dataclass
from os.path import commonprefix

from codey.providers.api_codec import ApiCodec
from codey.providers.base import ResultMetadata, result_metadata_from_text
from codey.providers.context_ledger import digest
from codey.types.json_value import JsonValue, decode_json_object, decode_json_value, require_json_strings


@dataclass(frozen=True)
class ContextRange:
    start: int
    end: int
    source_digest: str


def _unrecoverable_output(item: dict[str, object], result_refs: frozenset[str]) -> bool:
    """A summary must not discard a body with no currently usable reread path."""
    from codey.providers.text_result_views import TEXT_RESULTS_KEY, decode_text_result_spans

    if item.get("role") == "tool" or item.get("type") == "function_call_output":
        text = item.get("content") if item.get("role") == "tool" else item.get("output")
        metadata = result_metadata_from_text(text) if isinstance(text, str) else None
        return metadata is not None and metadata.result_ref not in result_refs
    if TEXT_RESULTS_KEY in item:
        text = item.get("content")
        if not isinstance(text, str):
            raise ValueError("text result view requires text content")
        return any(span.result_ref not in result_refs for span in decode_text_result_spans(text, item[TEXT_RESULTS_KEY]))
    return False


def select_range(codec: ApiCodec, items: list[dict[str, object]],
                 checkpoints: frozenset[str] = frozenset(), *, merge_checkpoints: bool = False,
                 recent_start: int | None = None, result_refs: frozenset[str] | None = None) -> ContextRange | None:
    spans = codec.closed_spans(items)
    latest_user = max((i for i, item in enumerate(items) if item.get("role") == "user"), default=-1)
    latest_tool = next(((start, end) for start, end in reversed(spans)
                        if any(item.get("tool_calls") or item.get("type") == "function_call"
                               for item in items[start:end])), None)
    # Existing verified segments stay intact; only newly closed work is summarized.
    eligible = [(start, end) for start, end in spans[:-1]
                if not start <= latest_user < end and (start, end) != latest_tool
                and (recent_start is None or end <= recent_start)
                and not any(digest(item) in checkpoints for item in items[start:end])
                and (result_refs is None or not any(_unrecoverable_output(item, result_refs)
                                                    for item in items[start:end]))]
    runs: list[tuple[int, int]] = []
    for start, end in eligible:
        if runs and runs[-1][1] == start:
            runs[-1] = (runs[-1][0], end)
        else:
            runs.append((start, end))
    if checkpoints:
        runs = [span for span in runs if len(str(items[span[0]:span[1]])) >= 256]
    if not runs:
        if merge_checkpoints:
            # Only a hard admission failure may merge old state. Re-read a
            # bounded pair of original segments, never repeatedly summarize all history.
            old = [(start, end) for start, end in spans[:-1]
                   if end == start + 1 and digest(items[start]) in checkpoints
                   and (result_refs is None or not _unrecoverable_output(items[start], result_refs))]
            for first, second in zip(old, old[1:], strict=False):
                if first[1] == second[0]:
                    return ContextRange(first[0], second[1], digest(items[first[0]:second[1]]))
        return None
    def span_size(span: tuple[int, int]) -> int:
        return len(str(items[span[0]:span[1]]))

    start, end = max(runs, key=span_size)
    return ContextRange(start, end, digest(items[start:end]))


def replace_range(items: list[dict[str, object]], selected: ContextRange,
                  replacement: list[dict[str, object]]) -> list[dict[str, object]]:
    if digest(items[selected.start:selected.end]) != selected.source_digest:
        raise ValueError("context source range changed")
    return copy.deepcopy(items[:selected.start] + replacement + items[selected.end:])


def restore_result_bodies(items: list[dict[str, object]], checkpoints: list[dict[str, object]],
                          result_refs: frozenset[str]) -> list[dict[str, object]]:
    """Expand archived views only where a receipt has lost its reread path."""
    originals: dict[str, list[dict[str, object]]] = {}

    def expand(source: list[dict[str, object]]) -> list[dict[str, object]]:
        return [entry for item in source for entry in originals.get(digest(item), [item])]

    for checkpoint in checkpoints:
        raw_source = checkpoint.get("source")
        if not isinstance(raw_source, list):
            raise ValueError("context checkpoint has no original source")
        source = [dict[str, object](decode_json_object(item)) for item in raw_source]
        if checkpoint.get("kind") == "output_reduction":
            raw_view = checkpoint.get("view_digests")
            view = require_json_strings(raw_view, "output view digests")
            if len(source) != len(view):
                raise ValueError("output reduction has no complete original body index")
            for identity, item in zip(view, source, strict=True):
                restored = expand([item])
                if isinstance(identity, str) and any(_unrecoverable_output(row, result_refs) for row in restored):
                    originals[identity] = restored
        elif isinstance(checkpoint_identity := checkpoint.get("item_digest"), str):
            restored = expand(source)
            if any(_unrecoverable_output(row, result_refs) for row in restored):
                originals[checkpoint_identity] = restored
    return copy.deepcopy(expand(items))


def _old_outputs(codec: ApiCodec, items: list[dict[str, object]]) -> Iterator[tuple[dict[str, object], str, str]]:
    spans = codec.closed_spans(items)
    latest_tool = next(((start, end) for start, end in reversed(spans)
                        if any(item.get("tool_calls") or item.get("type") == "function_call"
                               for item in items[start:end])), None)
    for start, end in spans[:-2]:
        if (start, end) == latest_tool:
            continue
        for item in items[start:end]:
            key = "content" if item.get("role") == "tool" else "output" if item.get("type") == "function_call_output" else ""
            text = item.get(key)
            if isinstance(text, str) and len(text) >= 2400:
                yield item, key, text


def _available_metadata(text: str, result_refs: frozenset[str]) -> ResultMetadata | None:
    # The runtime appends its receipt after the observed body. Quoted references
    # in that body must neither replace it nor rescue an unavailable final receipt.
    metadata = result_metadata_from_text(text)
    return metadata if metadata is not None and metadata.result_ref in result_refs else None


def _old_text_indices(codec: ApiCodec, items: list[dict[str, object]]) -> Iterator[int]:
    from codey.providers.text_result_views import TEXT_RESULTS_KEY

    # A later repair/request without new output must not make the current
    # observed source eligible for omission.
    latest = max((i for i, item in enumerate(items) if item.get(TEXT_RESULTS_KEY)), default=-1)
    for start, end in codec.closed_spans(items)[:-2]:
        for index in range(start, end):
            if index != latest and items[index].get(TEXT_RESULTS_KEY):
                yield index


def output_reduction_ready(codec: ApiCodec, items: list[dict[str, object]], result_refs: frozenset[str], *,
                           batch_receipts: bool) -> bool:
    """Treat unbacked old bodies losslessly; batch at least two backed bodies."""
    backed = 0
    for _, _, text in _old_outputs(codec, items):
        if _available_metadata(text, result_refs) is None:
            return True
        backed += 1
    from codey.providers.text_result_views import reducible_text_result_count

    backed += sum(reducible_text_result_count(decode_json_object(items[index]), result_refs) for index in _old_text_indices(codec, items))
    return batch_receipts and backed >= 2


def reduce_old_outputs(codec: ApiCodec, items: list[dict[str, object]], result_refs: frozenset[str]) -> list[dict[str, object]]:
    """Encode old structure losslessly; only omit bodies backed by a receipt."""
    result = copy.deepcopy(items)
    for item, key, text in _old_outputs(codec, result):
        metadata = _available_metadata(text, result_refs)
        if metadata is not None:
            item[key] = ("[Stored body omitted from this view]\n" + metadata.render() + "\n"
                         + "Read it with read_tool_result. Do not execute it again.")
        else:
            encoded = summary_source(text)
            if not isinstance(encoded, str):
                item[key] = _source_json(encoded)
    from codey.providers.text_result_views import reduce_text_results

    for index in _old_text_indices(codec, result):
        result[index] = dict[str, object](reduce_text_results(decode_json_object(result[index]), result_refs))
    return result


def validate_summary(text: str) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > 32000:
        raise ValueError("invalid work state answer")
    return text.strip()


def summary_source(value: object) -> JsonValue:
    return _summary_source(decode_json_value(value))


def _summary_source(value: JsonValue) -> JsonValue:
    """Encode repeated structure losslessly for model-facing sources and views."""
    if isinstance(value, str):
        if len(value) < 512:
            return value
        lines = value.splitlines(keepends=True)
        parts: list[JsonValue] = []
        pending: list[str] = []
        index = 0
        while index < len(lines):
            end = index + 1
            while end < len(lines) and lines[end] == lines[index]:
                end += 1
            count = end - index
            if count >= 3:
                parts.extend(_factor_lines(pending))
                pending = []
                parts.append({"repeat": count, "text": lines[index]})
            else:
                pending.extend(lines[index:end])
                if len(pending) >= 64:
                    parts.extend(_factor_lines(pending))
                    pending = []
            index = end
        parts.extend(_factor_lines(pending))
        encoded: JsonValue = {"text_encoding": "Concatenate parts. Templates emit prefix+value+suffix for each value; repeats emit text count times.",
                   "parts": parts}
        return encoded if len(_source_json(encoded)) < len(_source_json(value)) * 0.8 else value
    if isinstance(value, list):
        return [summary_source(item) for item in value]
    if isinstance(value, dict):
        if value.get("type") in {"image_url", "input_image"}:
            return {"type": "image", "note": "An image was attached. Visual content is retained in the original input, not in this text summary."}
        return {key: summary_source(item) for key, item in value.items()}
    return value


def _source_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _line_template(lines: list[str]) -> JsonValue:
    if len(lines) < 4:
        return "".join(lines)
    prefix = commonprefix(lines)
    tails = [line[len(prefix):] for line in lines]
    suffix = commonprefix([tail[::-1] for tail in tails])[::-1]
    values = [tail[:-len(suffix)] if suffix else tail for tail in tails]
    template: JsonValue = {"prefix": prefix, "values": [value for value in values], "suffix": suffix}
    original = "".join(lines)
    return template if len(_source_json(template)) < len(_source_json(original)) else original


def _factor_lines(lines: list[str]) -> list[JsonValue]:
    if not lines:
        return []
    # A final unterminated line must not defeat an otherwise shared suffix.
    whole: list[JsonValue] = [_line_template(lines)]
    split: list[JsonValue] = [_line_template(lines[:-1]), lines[-1]] if len(lines) >= 5 else whole
    candidates: list[list[JsonValue]] = [whole, split]
    if len(lines) >= 8:
        middle = len(lines) // 2
        candidates.append(_factor_lines(lines[:middle]) + _factor_lines(lines[middle:]))
    def encoded_size(parts: list[JsonValue]) -> int:
        return len(_source_json(parts))

    return min(candidates, key=encoded_size)
