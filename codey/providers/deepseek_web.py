from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from codey import deepseek
from codey.browser import DEFAULT_PORT, DEFAULT_PROFILE, Session, open_deepseek
from codey.provider_diagnostics import ProviderFailure, run_provider_action


@dataclass
class DeepSeekWebProvider:
    session: Session

    name: ClassVar[str] = "DeepSeek Web"
    last_failure: ProviderFailure | None = None

    @classmethod
    def connect(
        cls,
        *,
        port: int = DEFAULT_PORT,
        profile: Path = DEFAULT_PROFILE,
    ) -> DeepSeekWebProvider:
        return cls(open_deepseek(port=port, profile=profile))

    @property
    def location(self) -> str:
        return self.session.page.url

    def new_chat(self) -> None:
        run_provider_action(
            self,
            action="new_chat",
            page=self.session.page,
            func=lambda: deepseek.new_chat(self.session.page),
        )

    def send(self, text: str, timeout: float | None = None) -> str:
        kwargs = {}
        if timeout is not None:
            kwargs["response_timeout"] = timeout
        return run_provider_action(
            self,
            action="send",
            page=self.session.page,
            func=lambda: deepseek.chat(self.session.page, text, **kwargs),
        )

    def close(self) -> None:
        self.session.close()
