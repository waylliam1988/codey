from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from codey import cancellation, qwen
from codey.browser import DEFAULT_PORT, DEFAULT_PROFILE, Session, open_qwen
from codey.provider_diagnostics import ProviderFailure, run_provider_action
from codey.provider_timeouts import start_deadline


@dataclass
class QwenWebProvider:
    session: Session

    name: ClassVar[str] = "Qwen Studio"
    last_failure: ProviderFailure | None = None

    @classmethod
    def connect(
        cls,
        *,
        port: int = DEFAULT_PORT,
        profile: Path = DEFAULT_PROFILE,
        open_if_missing: bool = True,
        bring_to_front: bool = True,
    ) -> QwenWebProvider:
        return cls(
            open_qwen(
                port=port,
                profile=profile,
                open_if_missing=open_if_missing,
                bring_to_front=bring_to_front,
            )
        )

    @property
    def location(self) -> str:
        return self.session.page.url

    def new_chat(self, timeout: float | None = None) -> None:
        kwargs = {} if timeout is None else {"timeout": timeout}
        with cancellation.deadline_scope(start_deadline(timeout)):
            run_provider_action(
                self,
                action="new_chat",
                page=self.session.page,
                func=lambda: qwen.new_chat(self.session.page, **kwargs),
            )

    def send(self, text: str, timeout: float | None = None) -> str:
        kwargs = {}
        if timeout is not None:
            kwargs["response_timeout"] = timeout
        with cancellation.deadline_scope(start_deadline(timeout)):
            return run_provider_action(
                self,
                action="send",
                page=self.session.page,
                func=lambda: qwen.chat(self.session.page, text, **kwargs),
            )

    def close(self) -> None:
        self.session.close()
