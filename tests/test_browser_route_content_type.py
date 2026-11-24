from codey.automation.browser_contracts import RouteDecision
from codey.automation.browser_routes import route_policy
from codey.types.json_value import decode_json_object
from tests.support.task_browser import browser_for


def _reply(route: RouteDecision) -> None:
    route.fulfill(
        content_type="text/html",
        headers={"Content-Type": "text/plain", "X-Fixture": "route"},
        body="<title>Route fixture</title><p>Rendered response</p>",
    )


def test_explicit_route_content_type_replaces_a_case_variant() -> None:
    policy = route_policy(_reply)
    result = policy({"url": "https://example.com/route"})
    headers = decode_json_object(result["headers"])
    assert headers == {"content-type": "text/html", "X-Fixture": "route"}


def test_explicit_route_content_type_reaches_the_actual_browser() -> None:
    browser = browser_for(_reply)
    try:
        assert browser.navigate("https://example.com/route").ok
        assert browser.inspect_state("document.contentType") == "text/html"
        assert browser.inspect_state("document.title") == "Route fixture"
    finally:
        browser.close()
