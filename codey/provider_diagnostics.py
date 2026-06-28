from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class ProviderFailure:
    model: str
    action: str
    url: str
    title: str
    message: str
    time: str

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
