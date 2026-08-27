from __future__ import annotations

from codey.research.http_redirects import close_response, is_redirect_status, redirect_target


def test_redirect_status_accepts_int_like_values_without_throwing() -> None:
    assert is_redirect_status(302)
    assert is_redirect_status("308")
    assert not is_redirect_status("not-a-status")
    assert not is_redirect_status(None)


def test_redirect_target_reads_common_location_header_casing() -> None:
    assert redirect_target("https://example.com/a/b", {"Location": "../next"}) == "https://example.com/next"
    assert redirect_target("https://example.com/a/b", {"location": "/next"}) == "https://example.com/next"
    assert redirect_target("https://example.com/a/b", {}) == ""


def test_close_response_is_best_effort() -> None:
    class BrokenClose:
        def close(self) -> None:
            raise OSError("already closed")

    close_response(BrokenClose())
