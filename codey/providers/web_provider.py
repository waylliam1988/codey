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
from typing import Any, ClassVar

from codey import browser
from codey.browser import DEFAULT_PORT, DEFAULT_PROFILE, Session
from codey.provider_diagnostics import ProviderFailure
from codey.providers.web_driver import run_web_new_chat, run_web_send


@dataclass(frozen=True)
class WebProviderSpec:
    """Everything that differed between the old per-provider wrappers."""

    provider_id: str
    name: str
    driver: Any                      # site-specific driver module
    opener_name: str                 # attribute on codey.browser
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
    ) -> "WebChatProvider":
        opener = getattr(browser, cls.spec.opener_name)
        return cls(opener(
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
        kwargs = {} if timeout is None else {"timeout": timeout}
        run_web_new_chat(
            self,
            page=self.session.page,
            func=lambda: self.spec.driver.new_chat(self.session.page, **kwargs),
            timeout=timeout,
        )

    def send(self, text: str, timeout: float | None = None) -> str:
        if self.spec.blank_message and not text.strip():
            raise ValueError(self.spec.blank_message)
        kwargs = {}
        if timeout is not None:
            kwargs["response_timeout"] = timeout
        return run_web_send(
            self,
            page=self.session.page,
            func=lambda: self.spec.driver.chat(self.session.page, text, **kwargs),
            response_timeout=timeout,
            grace=getattr(self.spec.driver, self.spec.grace_attr),
        )

    def close(self) -> None:
        self.session.close()


def _provider_class(spec: WebProviderSpec) -> type[WebChatProvider]:
    return type(
        f"_{spec.provider_id.capitalize()}WebProviderBase",
        (WebChatProvider,),
        {"spec": spec},
    )


from codey.providers.web_drivers import deepseek as _deepseek_driver  # noqa: E402
from codey.providers.web_drivers import glm as _glm_driver  # noqa: E402
from codey.providers.web_drivers import mimo as _mimo_driver  # noqa: E402
from codey.providers.web_drivers import qwen as _qwen_driver  # noqa: E402
from codey.providers.web_drivers import stepfun as _stepfun_driver  # noqa: E402


class DeepSeekWebProvider(_provider_class(WebProviderSpec(
    provider_id="deepseek",
    name="DeepSeek Web",
    driver=_deepseek_driver,
    opener_name="open_deepseek",
))):
    pass


class MimoWebProvider(_provider_class(WebProviderSpec(
    provider_id="mimo",
    name="Xiaomi MiMo Chat",
    driver=_mimo_driver,
    opener_name="open_mimo",
))):
    pass


class StepFunWebProvider(_provider_class(WebProviderSpec(
    provider_id="stepfun",
    name="StepFun Chat",
    driver=_stepfun_driver,
    opener_name="open_stepfun",
))):
    pass


class QwenWebProvider(_provider_class(WebProviderSpec(
    provider_id="qwen",
    name="Qwen Studio",
    driver=_qwen_driver,
    opener_name="open_qwen",
))):
    pass


class GlmWebProvider(_provider_class(WebProviderSpec(
    provider_id="glm",
    name="GLM",
    driver=_glm_driver,
    opener_name="open_glm",
    grace_attr="RESPONSE_TIMEOUT_GRACE",
    blank_message="GLM message cannot be blank",
))):
    pass


WEB_PROVIDER_CLASSES: dict[str, type[WebChatProvider]] = {
    "deepseek": DeepSeekWebProvider,
    "mimo": MimoWebProvider,
    "stepfun": StepFunWebProvider,
    "qwen": QwenWebProvider,
    "glm": GlmWebProvider,
}
