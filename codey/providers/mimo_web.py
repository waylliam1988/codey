from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from codey import mimo
from codey.browser import DEFAULT_PORT, DEFAULT_PROFILE, Session, open_mimo


@dataclass
class MimoWebProvider:
    session: Session

    name: ClassVar[str] = "Xiaomi MiMo Chat"

    @classmethod
    def connect(
        cls,
        *,
        port: int = DEFAULT_PORT,
        profile: Path = DEFAULT_PROFILE,
    ) -> MimoWebProvider:
        return cls(open_mimo(port=port, profile=profile))

    @property
    def location(self) -> str:
        return self.session.page.url

    def new_chat(self) -> None:
        mimo.new_chat(self.session.page)

    def send(self, text: str, timeout: float | None = None) -> str:
        kwargs = {}
        if timeout is not None:
            kwargs["response_timeout"] = timeout
        return mimo.chat(self.session.page, text, **kwargs)

    def close(self) -> None:
        self.session.close()
