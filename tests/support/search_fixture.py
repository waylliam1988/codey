"""Engine-shaped pages served through the real task browser's policy transport."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from codey.automation.browser_contracts import RouteDecision
from codey.research.search_contracts import SearchOutcome, complete_search
from codey.research.search_provenance import SearchAttempt
from codey.types.json_value import decode_json_object


def search_outcome(query: str, rows: Sequence[Mapping[str, object]], *, limit: int = 8) -> SearchOutcome:
    """Build fixture data through the production contract and normalization."""
    return complete_search(query, [decode_json_object(row) for row in rows],
                           SearchAttempt(query, "fixture"), limit, explicit_empty=not rows)

FORM = """<form action='/search'><input id='sb_form_q' name='q' value='{query}'>
<input id='sb_form_go' type='submit' style='width:0;height:0;border:0;padding:0'>
<label for='sb_form_go'>Search</label><input type='hidden' name='form' value='fixture'></form>"""
RESULT = """<li class='b_algo'><h2><a href='https://example.com/current'>Current document</a></h2>
<div class='b_caption'><p>Observed source material about captcha and access denied.</p></div></li>"""


@dataclass
class SearchFixture:
    mode: str = "hits"
    delay_ms: int = 0
    requests: list[str] = field(default_factory=list)

    def route(self, reply: RouteDecision) -> None:
        url = reply.request.url
        self.requests.append(url)
        parsed = urlsplit(url)
        if parsed.hostname == "www.bing.com":
            # A new navigation stays on the fixture's interception boundary;
            # Playwright does not route later hops of a fulfilled HTTP redirect.
            reply.fulfill(content_type="text/html", body="<script>location.replace('https://cn.bing.com/')</script>")
            return
        query = parse_qs(parsed.query).get("q", [""])[0]
        # Fixture queries use plain text; escape attributes just as a real engine does.
        from html import escape

        body = FORM.format(query=escape(query, quote=True))
        if query:
            if self.mode == "challenge":
                body += "<div id='b_captcha'>Verify you are human</div>"
            elif self.mode == "empty":
                body += "<ol id='b_results'><li class='b_no'>No results found</li></ol>"
            elif self.mode == "structure":
                body += "<ol id='b_results'><li class='b_algo'>Changed result structure</li></ol>"
            else:
                body += "<header>Domestic International Web Images Maps News Travel</header><ol id='b_results'></ol>"
                if self.mode == "hits":
                    import json

                    body += "<script>setTimeout(()=>document.querySelector('#b_results').innerHTML=" + json.dumps(RESULT) + "," + str(self.delay_ms) + ")</script>"
        reply.fulfill(status=503 if self.mode == "http_error" and query else 200,
                      content_type="text/html; charset=utf-8", body="<html><body>" + body + "</body></html>")
