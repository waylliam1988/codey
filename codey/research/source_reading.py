"""One bounded web-source read renderer for fresh and captured acquisitions."""
from __future__ import annotations

from codey.research.ledger import ResearchLedger
from codey.research.source_gateway import OPEN_DEFAULT_LIMIT, OPEN_MAX_LIMIT, OPEN_MIN_LIMIT, OpenedSource, _as_int
from codey.research.source_rendering import render_opened_source
from codey.research.tool_output import ResearchToolOutput


def render_source_read(outcome: OpenedSource, ledger: ResearchLedger, *, offset: int, limit: int,
                       coherent: bool = False) -> ResearchToolOutput:
    if outcome.status == "error":
        return ResearchToolOutput(f"ERROR: {outcome.detail}", ok=False,
                                  acquisition_failed=outcome.acquisition_failed, acquisition=outcome.acquisition)
    if outcome.status == "skipped":
        return ResearchToolOutput(f"SKIPPED: {outcome.detail}", ok=False,
                                  acquisition_failed=outcome.acquisition_failed, acquisition=outcome.acquisition)
    document = outcome.document
    assert document is not None
    offset = max(0, _as_int(offset, 0))
    if offset >= len(document.text):
        return ResearchToolOutput(
            f"ERROR: source offset={offset} is at or beyond the end of available extracted text "
            f"(length={len(document.text)} characters). No passage was read. "
            "Do not advance the offset; use source_search on an earlier section or investigate another source.",
            ok=False, acquisition=outcome.acquisition)
    # Leave room for exact passage labels, metadata and source boundaries.
    limit = min(OPEN_MAX_LIMIT - 2500, max(OPEN_MIN_LIMIT, _as_int(limit, OPEN_DEFAULT_LIMIT)))
    from codey.research.passages import describe_captured_window, render_passage_window, restore_passages

    observation = outcome.observation
    passages = describe_captured_window(observation, offset, limit, coherent=coherent)
    if not passages:
        return ResearchToolOutput("ERROR: the requested source window contains no readable text; no passage was read.",
                                  ok=False, acquisition=outcome.acquisition)
    window = render_passage_window(document, passages)
    next_offset = passages[-1]["end"]
    more = next_offset < len(document.text)
    body = render_opened_source(
        document,
        window,
        more_offset=next_offset if more else None,
        read_window=(passages[0]["start"], passages[-1]["end"]),
    )
    if len(body) > OPEN_MAX_LIMIT:
        # A clipped body must not authorize a partly unseen passage. Drop
        # whole trailing passages and rerender their accurate next offset.
        while len(passages) > 1 and len(body) > OPEN_MAX_LIMIT:
            next_offset = passages.pop()["start"]
            window = render_passage_window(document, passages)
            body = render_opened_source(document, window, more_offset=next_offset,
                read_window=(passages[0]["start"], passages[-1]["end"]))
        if len(body) > OPEN_MAX_LIMIT:
            return ResearchToolOutput("ERROR: source metadata exceeds the readable window budget; no passage was read.",
                                      ok=False, acquisition=outcome.acquisition)
    restore_passages(ledger, observation, passages)
    full = render_opened_source(document, document.text)
    return ResearchToolOutput(model_text=body, receipt_text=full, read_passages=tuple(passages),
                              read_observation=observation, ok=True, acquisition=outcome.acquisition)

