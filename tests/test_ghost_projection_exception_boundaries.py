from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from codey.operations import ghost_context


def test_ghost_directive_does_not_hide_unexpected_projection_errors() -> None:
    state = SimpleNamespace(ghost_hebbian=object())
    with mock.patch.object(
        ghost_context,
        "build_ghost_directive",
        side_effect=RuntimeError("projection bug"),
    ), pytest.raises(RuntimeError, match="projection bug"):
        ghost_context.ghost_directive(state)


def test_ghost_continuity_does_not_hide_unexpected_projection_errors() -> None:
    state = SimpleNamespace(ghost_continuity=object())
    with mock.patch.object(
        ghost_context,
        "build_ghost_continuity",
        side_effect=RuntimeError("projection bug"),
    ), pytest.raises(RuntimeError, match="projection bug"):
        ghost_context.ghost_continuity(state)


def test_ghost_experiences_still_fails_closed_for_corrupt_observation_storage() -> None:
    state = SimpleNamespace(
        ghost_inbox=SimpleNamespace(learning_enabled=lambda: True),
        ghost_observations=SimpleNamespace(
            read_committed=mock.Mock(side_effect=OSError("observations unavailable")),
        ),
    )
    assert ghost_context.ghost_experiences(state, session_id="s", query="hello") == ""
