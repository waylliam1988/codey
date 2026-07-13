from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar
from urllib.parse import urlparse

from codey import cancellation


T = TypeVar("T")
FAILURE_TRANSIENT = "transient"
FAILURE_RATE_LIMITED = "rate_limited"
FAILURE_CONTROL_MISSING = "control_missing"
FAILURE_SUBMISSION_UNCERTAIN = "submission_uncertain"
FAILURE_RESPONSE_MISSING = "response_missing"
FAILURE_AUTHENTICATION_REQUIRED = "authentication_required"
FAILURE_CHALLENGE_REQUIRED = "challenge_required"

FAILURE_KINDS = frozenset({
    FAILURE_TRANSIENT,
    FAILURE_RATE_LIMITED,
    FAILURE_CONTROL_MISSING,
    FAILURE_SUBMISSION_UNCERTAIN,
    FAILURE_RESPONSE_MISSING,
    FAILURE_AUTHENTICATION_REQUIRED,
    FAILURE_CHALLENGE_REQUIRED,
})


class ControlMissing(TimeoutError):
    provider_failure_kind = FAILURE_CONTROL_MISSING


class ResponseMissing(TimeoutError):
    provider_failure_kind = FAILURE_RESPONSE_MISSING


class RateLimited(TimeoutError):
    provider_failure_kind = FAILURE_RATE_LIMITED


class AuthenticationRequired(RuntimeError):
    provider_failure_kind = FAILURE_AUTHENTICATION_REQUIRED


class ChallengeRequired(RuntimeError):
    provider_failure_kind = FAILURE_CHALLENGE_REQUIRED


@dataclass(frozen=True)
class ProviderFailure:
    model: str
    action: str
    url: str
    title: str
    message: str
    time: str
    kind: str = FAILURE_TRANSIENT

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class ProviderActionError(RuntimeError):
    """A provider-page failure captured at the action boundary."""

    def __init__(self, failure: ProviderFailure) -> None:
        self.failure = failure
        super().__init__(f"{failure.model} {failure.action} failed ({failure.kind})")


def capture_provider_failure(
    *,
    model: str,
    action: str,
    page: Any,
    error: BaseException,
    now: datetime | None = None,
) -> ProviderFailure:
    """Build a small failure record without inspecting page contents."""
    return ProviderFailure(
        model=model,
        action=action,
        url=_safe_page_value(page, "url"),
        title=_safe_page_title(page),
        message=str(error),
        time=(now or datetime.now(timezone.utc)).isoformat(),
        kind=_failure_kind(error),
    )


def run_provider_action(
    provider: Any,
    *,
    action: str,
    page: Any,
    func: Callable[[], T],
) -> T:
    try:
        guard_provider_page(page)
        result = func()
    except cancellation.TaskCancelled:
        raise
    except ProviderActionError:
        raise
    except Exception as exc:
        failure = capture_provider_failure(
            model=str(getattr(provider, "name", "")),
            action=action,
            page=page,
            error=exc,
        )
        provider.last_failure = failure
        raise ProviderActionError(failure) from exc
    provider.last_failure = None
    return result


def _failure_kind(error: BaseException) -> str:
    kind = str(getattr(error, "provider_failure_kind", FAILURE_TRANSIENT))
    return kind if kind in FAILURE_KINDS else FAILURE_TRANSIENT


def guard_provider_page(page: Any) -> None:
    """Reject explicit login or challenge pages before control recovery starts."""
    path = urlparse(_safe_page_value(page, "url")).path.lower()
    if any(marker in path for marker in ("/login", "/signin", "/sign-in")):
        raise AuthenticationRequired("provider login is required")
    if _visible_selector(page, 'input[type="password"]'):
        raise AuthenticationRequired("provider login is required")
    if any(
        _visible_selector(page, selector)
        for selector in (
            'iframe[src*="captcha"]',
            'iframe[src*="challenge"]',
            "[data-sitekey]",
        )
    ):
        raise ChallengeRequired("provider challenge requires user action")


def _visible_selector(page: Any, selector: str) -> bool:
    try:
        locator = page.locator(selector)
        if not locator.count():
            return False
        return bool(locator.first.is_visible())
    except Exception:
        return False


def _safe_page_value(page: Any, attr: str) -> str:
    try:
        value = getattr(page, attr, "")
    except Exception:
        return ""
    if callable(value):
        try:
            value = value()
        except Exception:
            return ""
    return str(value or "")


def _safe_page_title(page: Any) -> str:
    try:
        title = page.title
    except Exception:
        return ""
    try:
        value = title() if callable(title) else title
    except Exception:
        return ""
    return str(value or "")
