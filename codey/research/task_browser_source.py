"""Search and rendered source acquisition on the single task-browser runtime."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from codey.automation.browser_contracts import BrowserAction
from codey.automation.browser_session import BrowserSession
from codey.research import source_download
from codey.research.browser_page import _validate_page_status, validate_browser_capture
from codey.research.search_contracts import SearchOutcome, failed_search
from codey.research.search_page import OBSERVE_SEARCH, SearchObservation
from codey.research.search_profiles import SearchProfile, load_profiles
from codey.research.search_provenance import EXCLUDED_SEARCH_REGIONS, SearchAttempt
from codey.research.source_acquisition import with_attempt
from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject, decode_json_object, require_json_string

_FETCH_SETTLE_TIMEOUT_SECONDS = 8.0


@dataclass
class _SearchCapture:
    observation: SearchObservation | None = None
    diagnostics: tuple[JsonObject, ...] = ()
    phase: str = "homepage"


class TaskBrowserSource:
    name = "browser"

    def __init__(self, browser: BrowserSession, *, engine: str | None = None,
                 profiles: tuple[SearchProfile, ...] | None = None, owns_browser: bool = False) -> None:
        self.browser = browser
        self.engine = engine
        self.profiles = load_profiles() if profiles is None else profiles
        self._owns_browser = owns_browser
        if engine is not None and not any(profile.name == engine for profile in self.profiles):
            raise ValueError("unknown search engine: " + engine)
        self._search_target = ""
        self.last_outcome: SearchOutcome | None = None

    @property
    def search_attempts(self) -> tuple[SearchAttempt, ...]:
        return self.last_outcome.attempts if self.last_outcome is not None else ()

    @property
    def last_search_errors(self) -> list[dict[str, str]]:
        return self.last_outcome.errors if self.last_outcome is not None else []

    @property
    def last_search_failure(self) -> dict[str, str]:
        errors = self.last_search_errors
        return errors[-1] if errors else {}

    def close(self) -> None:
        if self._owns_browser:
            self.browser.close()

    def bind_browser(self, browser: BrowserSession) -> None:
        if self.browser is not browser:
            self.close()
            self.browser = browser
            self._search_target = ""
        self._owns_browser = False

    def fetch(self, url: str) -> dict[str, object]:
        if self._search_target and self.browser.resource_binding() is not None:
            binding = self.browser.resource_binding()
            assert binding is not None
            opened = (self.browser.tab_new(url) if binding.get("active_target") == self._search_target
                      else self.browser.navigate(url))
        else:
            opened = self.browser.navigate(url)
        if not opened.ok:
            delivery = opened.observation.get("delivery")
            return with_attempt({"status": "error", "url": url, "detail": opened.model_text,
                "failure_code": "request_failed", "delivery": "not_attempted" if delivery == "not_delivered" else
                "confirmed" if delivery in {"delivered", "verified"} else "unconfirmed"}, "browser", url)
        observed_status = opened.observation.get("http_status")
        invalid = _validate_page_status(url, observed_status if isinstance(observed_status, int) else None)
        if invalid is not None:
            return with_attempt(invalid, "browser", url)
        deadline = time.monotonic() + _FETCH_SETTLE_TIMEOUT_SECONDS
        while True:
            cancellation.check()
            page = self.browser.capture()
            validated = validate_browser_capture(page)
            if validated.get("status") == "ok" or validated.get("failure_code") != "page_not_loaded":
                break
            if time.monotonic() >= deadline:
                break
            cancellation.wait(min(0.15, max(0.0, deadline - time.monotonic())))
        status = page.get("http_status")
        final_url = str(page.get("url") or url)
        content_type = str(page.get("content_type") or "")
        if source_download._is_pdf_response(content_type, final_url) and (type(status) is not int or status < 400):
            discovered = with_attempt(source_download._pdf_download_sentinel(final_url, mime_type=content_type), "browser", url)
            return source_download.finish_pdf_download(discovered, url)
        if validated.get("failure_code") == "page_not_loaded":
            for event in self.browser.diagnostics():
                if event.get("kind") == "page_error":
                    validated = source_download._fetch_failure(final_url, "render_script_error: " + str(event.get("reason", "")),
                        code="render_script_error", delivery="confirmed")
                    break
                if (event.get("kind") == "request_failed" and event.get("resource_type") in {"script", "xhr", "fetch"}
                        and event.get("reason") != "net::ERR_ABORTED"):
                    validated = source_download._fetch_failure(final_url, "render_request_failed: " + str(event.get("reason", "")),
                        code="render_request_failed", delivery="confirmed")
                    break
        return with_attempt(validated, "browser", url)

    def search(self, query: str, *, limit: int = 8, profile: SearchProfile | None = None,
               deadline: float | None = None) -> SearchOutcome:
        active = profile or next(p for p in self.profiles if self.engine is None or p.name == self.engine)
        deadline = deadline if deadline is not None else time.monotonic() + 24
        self.last_outcome = None
        capture = _SearchCapture()
        try:
            with cancellation.deadline_scope(deadline):
                outcome = self._search_engine(query, limit, active, deadline, capture)
        except cancellation.TaskCancelled:
            raise
        except (TimeoutError, RuntimeError, ValueError, OSError) as exc:
            cancellation.check()
            kind = "search_timeout" if isinstance(exc, TimeoutError) else "search_observation_failed"
            outcome = failed_search(query, active.name, kind, capture.phase + ": " + str(exc)[:300],
                final_url=capture.observation.url if capture.observation is not None else "",
                diagnostics=capture.diagnostics)
        self.last_outcome = outcome
        return outcome

    def _activate_search(self, profile: SearchProfile) -> None:
        if self._search_target:
            switched = self.browser.action(BrowserAction.TAB_SWITCH, {"target_id": self._search_target})
            if switched.ok:
                self.browser.diagnostics(clear=True)
                return
            if switched.observation.get("reason") != "tab_unavailable":
                raise RuntimeError(switched.model_text)
        opened = self.browser.tab_new(profile.home_url)
        if not opened.ok:
            raise RuntimeError(opened.model_text)
        self._search_target = require_json_string(opened.observation["page_id"], "search target")

    def _observe(self, profile: SearchProfile, capture: _SearchCapture) -> SearchObservation:
        output = self.browser.evaluate("(" + OBSERVE_SEARCH + ")(" + json.dumps({
            "profile": profile.payload(), "excluded": EXCLUDED_SEARCH_REGIONS}) + ")", main_frame=True)
        if not output.ok:
            raise RuntimeError(output.model_text)
        observed = SearchObservation(decode_json_object(output.observation["value"]),
                                     decode_json_object(output.observation["document"]))
        capture.observation = observed
        capture.diagnostics = self.browser.diagnostics()
        return observed

    def _search_engine(self, query: str, limit: int, profile: SearchProfile, deadline: float,
                       capture: _SearchCapture) -> SearchOutcome:
        self._activate_search(profile)
        observed = self._observe(profile, capture)
        if not profile.allows(observed.url) or observed.value["form_ready"] is not True:
            opened = self.browser.navigate(profile.home_url)
            if not opened.ok:
                return failed_search(query, profile.name, "search_navigation_failed", opened.model_text)
        while True:
            observed = self._observe(profile, capture)
            if not profile.allows(observed.url):
                return failed_search(query, profile.name, "search_wrong_host", final_url=observed.url)
            if observed.value["challenge"] is True:
                return failed_search(query, profile.name, "search_challenge", final_url=observed.url,
                                     diagnostics=capture.diagnostics)
            if observed.value["form_ready"] is True:
                break
            cancellation.wait(min(0.1, max(0.0, deadline - time.monotonic())))
        submitted_document = observed.document
        capture.phase = "submission"
        submitted = self.browser.submit_form(profile.query_input, profile.submit, query, submitted_document)
        if not submitted.ok:
            failed_submission: JsonObject = {"kind": "submission_failed", "receipt": submitted.observation}
            return failed_search(query, profile.name, "search_submission_failed", submitted.model_text,
                                 diagnostics=(*capture.diagnostics, failed_submission))
        capture.phase = "results"
        while True:
            cancellation.check()
            observed = self._observe(profile, capture)
            remaining = deadline - time.monotonic()
            outcome = observed.classify(query, profile, limit, expired=remaining <= 0.2,
                diagnostics=capture.diagnostics, submitted_document=submitted_document)
            if outcome is not None:
                return outcome
            cancellation.wait(min(0.15, max(0.0, remaining - 0.1)))
