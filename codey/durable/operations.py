"""Domain operations join the kernel's existing mutation line."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Concatenate, ParamSpec, TypeVar

from codey.durable.session_kernel import SessionKernel

_Operations = TypeVar("_Operations", bound="SessionOperations")
_P = ParamSpec("_P")
_T = TypeVar("_T")


class SessionOperations:
    """Share a kernel; never own a second store, lock, or failure state."""

    def __init__(self, kernel: SessionKernel) -> None:
        self._kernel = kernel


def operation_line(
    method: Callable[Concatenate[_Operations, _P], _T],
) -> Callable[Concatenate[_Operations, _P], _T]:
    @wraps(method)
    def run(self: _Operations, /, *args: _P.args, **kwargs: _P.kwargs) -> _T:
        with self._kernel.control_line():
            return method(self, *args, **kwargs)

    return run
