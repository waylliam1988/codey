"""One DOM observation and one classification policy for browser search."""
from __future__ import annotations

from dataclasses import dataclass

from codey.research.search_contracts import SearchOutcome, complete_search, failed_search
from codey.research.search_profiles import SearchProfile
from codey.research.search_provenance import SearchAttempt, query_consistency
from codey.types.json_value import JsonObject, decode_json_object, require_json_integer, require_json_string
from codey.utils.refs import normalize_text

OBSERVE_SEARCH = r"""({profile, excluded}) => {
    const visible = el => !!el && el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0
        && el.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
    const text = el => (el?.innerText || '').replace(/\s+/g,' ').trim();
    const root = document.querySelector(profile.result_root);
    const blocks = Array.from(root?.querySelectorAll(profile.result_selector) || []).filter(el => visible(el) && !el.closest(excluded));
    const rows = [];
    for (const block of blocks.slice(0, 100)) {
        const link = block.querySelector(profile.link_selector);
        if (visible(link)) rows.push({url:link.href,title:text(link),snippet:text(block.querySelector(profile.snippet_selector))});
    }
    const empty = Array.from(document.querySelectorAll(profile.empty_selector)).find(visible);
    const challenge = Array.from(document.querySelectorAll(profile.challenge_selector)).some(visible);
    const input = document.querySelector(profile.query_input);
    return {url:location.href,query:input?.value ?? null,form_ready:visible(input),root:visible(root),
        blocks:blocks.length,rows,empty:text(empty),challenge,ready_state:document.readyState,
        text:text(root).slice(0,1600),http_status:performance.getEntriesByType('navigation')[0]?.responseStatus || null};
}"""


@dataclass(frozen=True)
class SearchObservation:
    value: JsonObject
    document: JsonObject

    @property
    def url(self) -> str:
        return require_json_string(self.value["url"], "page URL")

    def classify(self, query: str, profile: SearchProfile, limit: int, *, expired: bool = False,
                 diagnostics: tuple[JsonObject, ...] = (),
                 submitted_document: JsonObject | None = None) -> SearchOutcome | None:
        value = self.value
        if not profile.allows(self.url):
            return failed_search(query, profile.name, "search_wrong_host", final_url=self.url)
        if value["challenge"] is True:
            return failed_search(query, profile.name, "search_challenge", final_url=self.url, diagnostics=diagnostics)
        status = value["http_status"]
        if isinstance(status, int) and status >= 400:
            return failed_search(query, profile.name, "search_page_unavailable", str(status), final_url=self.url)
        consistent = query_consistency(self.url, query)
        observed_query = value["query"]
        input_matches = (observed_query is not None and
            normalize_text(require_json_string(observed_query, "observed query")) == normalize_text(query))
        if consistent is None and submitted_document is not None and self.document != submitted_document:
            consistent = input_matches
        if consistent is True and observed_query is not None and not input_matches:
            return failed_search(query, profile.name, "search_query_mismatch", final_url=self.url)
        # The filled homepage is not a completed query, even if it contains old results.
        if consistent is not True:
            if expired:
                return failed_search(query, profile.name, "search_query_mismatch", final_url=self.url,
                                     diagnostics=diagnostics)
            return None
        raw = value["rows"]
        if not isinstance(raw, list):
            raise ValueError("invalid observed search rows")
        rows = [decode_json_object(item) for item in raw]
        empty_text = require_json_string(value["empty"], "empty marker").casefold()
        empty = bool(empty_text) and any(marker in empty_text for marker in profile.empty_markers)
        attempt = SearchAttempt(query, profile.name, final_url=self.url, query_consistent=True,
            extraction_scope="result_regions", effective_query=query, diagnostics=diagnostics)
        if rows or empty:
            return complete_search(query, rows, attempt, limit, explicit_empty=empty)
        if expired:
            blocks = require_json_integer(value["blocks"], "result block count")
            kind = "search_structure_changed" if blocks else "search_page_blank"
            return failed_search(query, profile.name, kind, final_url=self.url, diagnostics=diagnostics)
        return None
