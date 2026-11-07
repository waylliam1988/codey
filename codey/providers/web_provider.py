"""One parameterized wrapper for every browser-backed chat provider.

The five former ``*_web.py`` wrappers were byte-identical except for the
driver module, opener function, display name, grace constant, and GLM's
blank-message guard. They now share this single implementation; each
provider keeps a named subclass so registry dispatch and diagnostics stay
readable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, cast

from codey.automation import browser
from codey.automation.browser import DEFAULT_PORT, DEFAULT_PROFILE, Session
from codey.providers.diagnostics import ProviderFailure
from codey.providers.web_driver import run_web_new_chat, run_web_send


@dataclass(frozen=True)
class WebProviderSpec:
    """Everything that differed between the old per-provider wrappers."""

    provider_id: str
    name: str
    driver: Any                      # site-specific driver module
    grace_attr: str = "TIMEOUT_GRACE"
    blank_message: str = ""          # non-empty: reject blank sends


class WebChatProvider:
    """Thin ChatProvider over one open browser tab and its driver module."""

    spec: ClassVar[WebProviderSpec]
    last_failure: ProviderFailure | None = None

    def __init__(self, session: Session) -> None:
        self.session = session
        self.last_failure: ProviderFailure | None = None
        self.name: str = type(self).spec.name
        self._attachment_binding: tuple[Any, str, str] | None = None
        self._attachment_entries: set[str] = set()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.session!r})"

    @classmethod
    def connect(
        cls,
        *,
        port: int = DEFAULT_PORT,
        profile: Path = DEFAULT_PROFILE,
        open_if_missing: bool = True,
        bring_to_front: bool = True,
        isolated: bool = False,
        fresh_tab: bool = False,
    ) -> WebChatProvider:
        return cls(browser.open_chat_page(
            browser.PROVIDER_START_URLS[cls.spec.provider_id],
            browser.PROVIDER_URL_CONTAINS[cls.spec.provider_id],
            port=port,
            profile=profile,
            open_if_missing=open_if_missing,
            bring_to_front=bring_to_front,
            isolated=isolated,
            fresh_tab=fresh_tab,
        ))

    @property
    def location(self) -> str:
        return self.session.page.url

    def new_chat(self, timeout: float | None = None) -> None:
        kwargs = dict[str, float]() if timeout is None else {"timeout": timeout}
        run_web_new_chat(
            self,
            page=self.session.page,
            func=lambda: self.spec.driver.new_chat(self.session.page, **kwargs),
            timeout=timeout,
        )

    def bind_input_attachments(self, durable: Any, session_id: str, task_id: str) -> None:
        self._attachment_binding = (durable, session_id, task_id)

    def send(self, text: str, timeout: float | None = None) -> str:
        if not isinstance(text, str):
            raise ValueError("message must be a string")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))):
            raise ValueError("timeout must be a finite number")
        if isinstance(timeout, float):
            import math as _math
            if not _math.isfinite(timeout) or timeout <= 0:
                raise ValueError("timeout must be a finite number")
        if self.spec.blank_message and not text.strip():
            raise ValueError(self.spec.blank_message)
        from codey.providers.request_context import clock_context

        entries: list[str] = []
        if self._attachment_binding is not None:
            from codey.providers.input_attachments import browser_attachment_text, pending_attachments

            items, entries = pending_attachments(*self._attachment_binding, self._attachment_entries)
            text = browser_attachment_text(self.spec.provider_id, text, items)
        text = clock_context() + "\n\n" + text
        kwargs = {}
        if timeout is not None:
            kwargs["response_timeout"] = timeout
        reply = cast(str, run_web_send(
            self,
            page=self.session.page,
            func=lambda: self.spec.driver.chat(self.session.page, text, **kwargs),
            response_timeout=timeout,
            grace=getattr(self.spec.driver, self.spec.grace_attr),
        ))
        self._attachment_entries.update(entries)
        return reply

    def close(self) -> None:
        self.session.close()


from codey.providers.web_drivers import deepseek as _deepseek_driver  # noqa: E402
from codey.providers.web_drivers import glm as _glm_driver  # noqa: E402
from codey.providers.web_drivers import mimo as _mimo_driver  # noqa: E402
from codey.providers.web_drivers import qwen as _qwen_driver  # noqa: E402
from codey.providers.web_drivers import stepfun as _stepfun_driver  # noqa: E402


class DeepSeekWebProvider(WebChatProvider):
    spec = WebProviderSpec(
        provider_id="deepseek",
        name="DeepSeek Web",
        driver=_deepseek_driver,
    )


class MimoWebProvider(WebChatProvider):
    spec = WebProviderSpec(
        provider_id="mimo",
        name="Xiaomi MiMo Chat",
        driver=_mimo_driver,
    )


class StepFunWebProvider(WebChatProvider):
    spec = WebProviderSpec(
        provider_id="stepfun",
        name="StepFun Chat",
        driver=_stepfun_driver,
    )


class QwenWebProvider(WebChatProvider):
    spec = WebProviderSpec(
        provider_id="qwen",
        name="Qwen Studio",
        driver=_qwen_driver,
    )


class GlmWebProvider(WebChatProvider):
    spec = WebProviderSpec(
        provider_id="glm",
        name="GLM",
        driver=_glm_driver,
        grace_attr="RESPONSE_TIMEOUT_GRACE",
        blank_message="GLM message cannot be blank",
    )


WEB_PROVIDER_CLASSES: dict[str, type[WebChatProvider]] = {
    "deepseek": DeepSeekWebProvider,
    "mimo": MimoWebProvider,
    "stepfun": StepFunWebProvider,
    "qwen": QwenWebProvider,
    "glm": GlmWebProvider,
}
