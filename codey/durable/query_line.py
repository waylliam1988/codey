"""Typed execution line shared by durable query facades."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

_T = TypeVar("_T")


class QueryLine(Protocol):
    def __call__(self, operation: Callable[[], _T], /) -> _T: ...


__all__ = ["QueryLine"]
