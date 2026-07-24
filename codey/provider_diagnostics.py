from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar
from urllib.parse import urlparse

from codey import cancellation
from codey.provider_flow import (
    STAGE_COMPLETION,
    STAGE_INPUT,
    STAGE_NEW_CHAT,
    STAGE_RETRY,
    STAGES,
)


T = TypeVar("T")
FAILURE_TRANSIENT = "transient"
FAILURE_RATE_LIMITED = "rate_limited"
FAILURE_CONTROL_MISSING = "control_missing"
FAILURE_SUBMISSION_UNCERTAIN = "submission_uncertain"
FAILURE_RESPONSE_MISSING = "response_missing"
FAILURE_READINESS_STALE = "readiness_stale"
FAILURE_AUTHENTICATION_REQUIRED = "authentication_required"
FAILURE_CHALLENGE_REQUIRED = "challenge_required"
MAX_FAILURE_FACTS = 12
MAX_FAILURE_FACT_VALUE = 160
ALLOWED_FAILURE_FACTS = frozenset({
    "composer_visible",
    "model_selector_text_present",
    "question_count",
    "response_count",
    "send_visible",
    "waited_for",
})

FAILURE_KINDS = frozenset({
    FAILURE_TRANSIENT,
    FAILURE_RATE_LIMITED,
    FAILURE_CONTROL_MISSING,
    FAILURE_SUBMISSION_UNCERTAIN,
    FAILURE_RESPONSE_MISSING,
    FAILURE_READINESS_STALE,
    FAILURE_AUTHENTICATION_REQUIRED,
    FAILURE_CHALLENGE_REQUIRED,
})


class ControlMissing(TimeoutError):
    provider_failure_kind = FAILURE_CONTROL_MISSING

    def __init__(self, message: str, *, stage: str = STAGE_INPUT) -> None:
        self.provider_failure_stage = stage
        super().__init__(message)


class ResponseMissing(TimeoutError):
    provider_failure_kind = FAILURE_RESPONSE_MISSING

    def __init__(self, message: str, *, stage: str = STAGE_COMPLETION) -> None:
        self.provider_failure_stage = stage
        super().__init__(message)


class ReadinessStale(TimeoutError):
    provider_failure_kind = FAILURE_READINESS_STALE
    provider_failure_stage = STAGE_NEW_CHAT

    def __init__(self, message: str, *, facts: dict[str, object] | None = None) -> None:
        self.provider_failure_facts = sanitize_failure_facts(facts)
        super().__init__(message)


class RateLimited(TimeoutError):
    provider_failure_kind = FAILURE_RATE_LIMITED
    provider_failure_stage = STAGE_RETRY


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
    stage: str = ""
    facts: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "facts", sanitize_failure_facts(self.facts))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        facts = sanitize_failure_facts(payload.pop("facts", {}))
        if facts:
            payload["facts"] = facts
        return payload


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
        stage=_failure_stage(error, action),
        facts=_failure_facts(error),
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


def _failure_stage(error: BaseException, action: str) -> str:
    stage = str(getattr(error, "provider_failure_stage", "") or "")
    if stage in STAGES:
        return stage
    return STAGE_NEW_CHAT if action == "new_chat" else ""


def _failure_facts(error: BaseException) -> dict[str, object]:
    return sanitize_failure_facts(getattr(error, "provider_failure_facts", None))


def sanitize_failure_facts(value: object) -> dict[str, object]:
    """Keep self-repair diagnostics small and free of page/user content."""
    if not isinstance(value, dict):
        return {}
    facts: dict[str, object] = {}
    for raw_key, raw_value in value.items():
        if len(facts) >= MAX_FAILURE_FACTS:
            break
        key = _safe_fact_key(raw_key)
        if not key:
            continue
        fact_value = _safe_fact_value(raw_value)
        if fact_value is None:
            continue
        facts[key] = fact_value
    return facts


def _safe_fact_key(value: object) -> str:
    key = str(value or "").strip()
    return key if key in ALLOWED_FAILURE_FACTS else ""


def _safe_fact_value(value: object) -> object | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = " ".join(value.strip().split())
        if not text or _unsafe_fact_text(text):
            return None
        return text[:MAX_FAILURE_FACT_VALUE]
    return None


def _unsafe_fact_text(value: str) -> bool:
    lower = value.lower()
    if any(
        term in lower
        for term in ("<html", "<body", "authorization:", "bearer ", "http://", "https://")
    ):
        return True
    return False


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
