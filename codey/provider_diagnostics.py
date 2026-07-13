from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from codey import cancellation


T = TypeVar("T")
FAILURE_TRANSIENT = "transient"
FAILURE_CONTROL_MISSING = "control_missing"
FAILURE_SUBMISSION_UNCERTAIN = "submission_uncertain"
FAILURE_RESPONSE_MISSING = "response_missing"


class ControlMissing(TimeoutError):
    provider_failure_kind = FAILURE_CONTROL_MISSING


class ResponseMissing(TimeoutError):
    provider_failure_kind = FAILURE_RESPONSE_MISSING


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
        kind=str(getattr(error, "provider_failure_kind", FAILURE_TRANSIENT)),
    )


def run_provider_action(
    provider: Any,
    *,
    action: str,
    page: Any,
    func: Callable[[], T],
) -> T:
    try:
        return func()
    except cancellation.TaskCancelled:
        raise
    except Exception as exc:
        provider.last_failure = capture_provider_failure(
            model=str(getattr(provider, "name", "")),
            action=action,
            page=page,
            error=exc,
        )
        raise


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
