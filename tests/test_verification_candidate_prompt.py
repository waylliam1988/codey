from __future__ import annotations

from types import SimpleNamespace

import pytest

from codey.agents.request import AgentRequest
from codey.completion.verification_policy import VerificationCandidate, render_verification_guidance
from codey.operations.project_adapter import _project_context


def test_project_writer_marks_preferred_verification_candidate(tmp_path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    request = AgentRequest(
        provider=SimpleNamespace(),
        project=tmp_path,
        task="change app.py",
        verification_candidates=(
            VerificationCandidate("python -m pytest", ".", "pytest.ini"),
            VerificationCandidate("python -m unittest discover", ".", "unittest test files"),
        ),
    )

    text = _project_context(request)

    assert "Preferred verification command after edits" in text
    assert "python -m pytest" in text
    assert "source: pytest.ini" in text
    assert "python -m unittest discover" not in text
    assert len(request.verification_candidates) == 2


@pytest.mark.parametrize("task, profile", [
    ("Change app.py. Do not run tests.", "coding_writer"),
    ("Plan a change to app.py without editing.", "planning_readonly"),
])
def test_context_does_not_suggest_verification_when_forbidden(tmp_path, task, profile) -> None:
    request = AgentRequest(provider=SimpleNamespace(), project=tmp_path, task=task,
                           permission_profile=profile,
                           verification_candidates=(VerificationCandidate("python -m pytest"),))
    text = _project_context(request)
    assert "Preferred verification command after edits:" not in text
    assert "Verification candidates (no unique preference):" not in text


def test_guidance_preserves_checks_for_separate_project_scopes() -> None:
    text = render_verification_guidance((
        VerificationCandidate("python -m pytest", "backend", "pytest.ini"),
        VerificationCandidate("python -m unittest discover", "backend", "tests directory"),
        VerificationCandidate("npm test", "frontend", "package.json script test"),
    ))
    assert "python -m pytest" in text and "cwd: backend" in text
    assert "npm test" in text and "cwd: frontend" in text
    assert "python -m unittest discover" not in text


def test_guidance_keeps_different_runtimes_in_the_same_directory() -> None:
    text = render_verification_guidance((
        VerificationCandidate("python -m pytest", ".", "pytest.ini"),
        VerificationCandidate("python -m unittest discover", ".", "tests directory"),
        VerificationCandidate("npm test", ".", "package.json script test"),
    ))
    assert "python -m pytest" in text and "npm test" in text
    assert "python -m unittest discover" not in text


def test_guidance_does_not_invent_preference_for_equal_rank_checks() -> None:
    text = render_verification_guidance((
        VerificationCandidate("python -m pytest", ".", "pytest.ini"),
        VerificationCandidate("pytest", ".", "config"),
    ))
    assert "no unique preference" in text
    assert "- python -m pytest" in text and "- pytest" in text


@pytest.mark.parametrize("selected", [
    VerificationCandidate("python -m unittest discover", ".", "configured", source_priority=50),
    VerificationCandidate("python -m unittest discover", ".", "history", previously_passed=True),
])
def test_guidance_respects_project_configuration_and_successful_history(selected) -> None:
    text = render_verification_guidance((
        VerificationCandidate("python -m pytest", ".", "pytest.ini"), selected,
    ))
    assert "python -m unittest discover" in text
    assert "python -m pytest" not in text


def test_guidance_bounds_commands_and_collapses_untrusted_newlines() -> None:
    candidates = tuple(VerificationCandidate("pytest\n" + "x" * 300, f"pkg{i}", "source\r\ninjection")
                       for i in range(8))
    text = render_verification_guidance(candidates)
    assert len([line for line in text.splitlines() if line.startswith("- ")]) == 5
    assert "\r" not in text and "\ninjection" not in text
    assert "x" * 241 not in text


def test_ab_rewrites_both_prompt_shapes_without_changing_other_context() -> None:
    from tests.manual.verification_candidate_prompt_ab import _flat_candidate_prompt, _rewrite_candidate_prompt

    candidates = (VerificationCandidate("python -m pytest", ".", "pytest.ini"),)
    original = "Project context\n\nTrusted verification candidates after edits:\n- python -m pytest (cwd: .)"
    treatment = _rewrite_candidate_prompt(original, candidates)
    assert treatment == "Project context\n\n" + render_verification_guidance(candidates)
    assert _flat_candidate_prompt(treatment, candidates) == original
    with pytest.raises(ValueError, match="section missing"):
        _rewrite_candidate_prompt("missing context", candidates)
