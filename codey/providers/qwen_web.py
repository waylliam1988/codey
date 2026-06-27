from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from codey import qwen
from codey.browser import DEFAULT_PORT, DEFAULT_PROFILE, Session, open_qwen


@dataclass
class QwenWebProvider:
    session: Session

    name: ClassVar[str] = "Qwen Studio"

    @classmethod
    def connect(
        cls,
        *,
        port: int = DEFAULT_PORT,
        profile: Path = DEFAULT_PROFILE,
    ) -> QwenWebProvider:
        return cls(open_qwen(port=port, profile=profile))

    @property
    def location(self) -> str:
        return self.session.page.url

    def new_chat(self) -> None:
        qwen.new_chat(self.session.page)

    def send(self, text: str, timeout: float | None = None) -> str:
        kwargs = {}
        if timeout is not None:
            kwargs["response_timeout"] = timeout
        return qwen.chat(self.session.page, text, **kwargs)

    def close(self) -> None:
        self.session.pw.stop()
