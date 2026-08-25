from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from codey import glm
from codey.browser import DEFAULT_PORT, DEFAULT_PROFILE, Session, open_glm
from codey.provider_diagnostics import ProviderFailure
from codey.providers.web_driver import run_web_new_chat, run_web_send


@dataclass
class GlmWebProvider:
    session: Session

    name: ClassVar[str] = "GLM"
    last_failure: ProviderFailure | None = None

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
    ) -> GlmWebProvider:
        return cls(
            open_glm(
                port=port,
                profile=profile,
                open_if_missing=open_if_missing,
                bring_to_front=bring_to_front,
                isolated=isolated,
                fresh_tab=fresh_tab,
            )
        )

    @property
    def location(self) -> str:
        return self.session.page.url

    def new_chat(self, timeout: float | None = None) -> None:
        kwargs = {} if timeout is None else {"timeout": timeout}
        run_web_new_chat(
            self,
            page=self.session.page,
            func=lambda: glm.new_chat(self.session.page, **kwargs),
            timeout=timeout,
        )

    def send(self, text: str, timeout: float | None = None) -> str:
        if not text.strip():
            raise ValueError("GLM message cannot be blank")
        kwargs = {}
        if timeout is not None:
            kwargs["response_timeout"] = timeout
        return run_web_send(
            self,
            page=self.session.page,
            func=lambda: glm.chat(self.session.page, text, **kwargs),
            response_timeout=timeout,
            grace=glm.RESPONSE_TIMEOUT_GRACE,
        )

    def close(self) -> None:
        self.session.close()
