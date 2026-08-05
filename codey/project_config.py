"""Project-local configuration parsing for bounded Codey hints.

The config is a project fact source, not an authorization source. Commands and
paths parsed here are re-checked by the normal runtime policy before they can
affect tool prompts or execution.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable

from codey.provider_capabilities import PROVIDER_CAPABILITIES


PROJECT_CONFIG_RELATIVE_PATH = ".codey/config.json"
PROJECT_CONFIG_SCHEMA_VERSION = 1
MAX_PROJECT_CONFIG_BYTES = 64 * 1024
MAX_VERIFICATION_COMMANDS = 12
MAX_COMMAND_CHARS = 300
MAX_LABEL_CHARS = 80
MAX_IGNORED_PATHS = 64
MAX_WARNINGS = 8
MAX_WARNING_CHARS = 180
MIN_PROJECT_MAP_CHARS = 1_000
PROVIDER_POLICY_MODES = frozenset({
    "chat",
    "research",
    "project",
    "review",
    "hybrid",
    "planning",
})


@dataclass(frozen=True)
class ProjectVerificationCommand:
    command: str
    cwd: str = "."
    label: str = ""


@dataclass(frozen=True)
class ProjectProviderPreference:
    mode: str
    provider_id: str


@dataclass(frozen=True)
class ProjectContextBudgetHints:
    project_map_chars: int | None = None


@dataclass(frozen=True)
class ProjectConfig:
    verification_commands: tuple[ProjectVerificationCommand, ...] = ()
    ignored_paths: tuple[str, ...] = ()
    context_budget_hints: ProjectContextBudgetHints = field(
        default_factory=ProjectContextBudgetHints
    )
    preferred_providers: tuple[ProjectProviderPreference, ...] = ()


@dataclass(frozen=True)
class ProjectConfigLoadResult:
    config: ProjectConfig = field(default_factory=ProjectConfig)
    warnings: tuple[str, ...] = ()
    path: str = ""
    warning_count: int = 0


def load_project_config(project: str | Path) -> ProjectConfigLoadResult:
    root = Path(project).expanduser().resolve()
    path = root / PROJECT_CONFIG_RELATIVE_PATH
    path_text = path.as_posix()
    if not path.exists():
        return ProjectConfigLoadResult(path=path_text)
    warnings: list[str] = []
    try:
        if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
            return ProjectConfigLoadResult(
                warnings=("ignored .codey/config.json because it is not a regular project file",),
                path=path_text,
                warning_count=1,
            )
        resolved = path.resolve()
        if root != resolved and root not in resolved.parents:
            return ProjectConfigLoadResult(
                warnings=("ignored .codey/config.json because it resolves outside the project",),
                path=path_text,
                warning_count=1,
            )
        size = path.stat().st_size
        if size > MAX_PROJECT_CONFIG_BYTES:
            return ProjectConfigLoadResult(
                warnings=(f"ignored .codey/config.json because it exceeds {MAX_PROJECT_CONFIG_BYTES} bytes",),
                path=path_text,
                warning_count=1,
            )
        raw = path.read_bytes()
    except OSError as exc:
        return ProjectConfigLoadResult(
            warnings=(_warning(f"could not read .codey/config.json: {exc}"),),
            path=path_text,
            warning_count=1,
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return ProjectConfigLoadResult(
            warnings=(_warning(f"ignored .codey/config.json because it is invalid JSON: {exc}"),),
            path=path_text,
            warning_count=1,
        )
    if not isinstance(data, dict):
        return ProjectConfigLoadResult(
            warnings=("ignored .codey/config.json because the root value is not an object",),
            path=path_text,
            warning_count=1,
        )
    if data.get("schema_version") != PROJECT_CONFIG_SCHEMA_VERSION:
        return ProjectConfigLoadResult(
            warnings=(f"ignored .codey/config.json because schema_version must be {PROJECT_CONFIG_SCHEMA_VERSION}",),
            path=path_text,
            warning_count=1,
        )

    config = ProjectConfig(
        verification_commands=_parse_verification_commands(data.get("verification"), warnings),
        ignored_paths=_parse_ignored_paths(data.get("scan"), warnings),
        context_budget_hints=_parse_context_budget_hints(data.get("context"), warnings),
        preferred_providers=_parse_provider_preferences(data.get("providers"), warnings),
    )
    return ProjectConfigLoadResult(
        config=config,
        warnings=tuple(warnings[:MAX_WARNINGS]),
        path=path_text,
        warning_count=len(warnings),
    )


def render_project_config_warnings(result: ProjectConfigLoadResult) -> str:
    warnings = tuple(_warning(item) for item in result.warnings if str(item).strip())
    if not warnings:
        return ""
    lines = [f"- {item}" for item in warnings[:MAX_WARNINGS]]
    total = max(result.warning_count, len(warnings))
    if total > MAX_WARNINGS:
        lines.append(f"- omitted {total - MAX_WARNINGS} more config warning(s)")
    return "\n".join(lines)


def path_matches_ignored_prefix(rel: str, ignored_paths: Iterable[str]) -> bool:
    normalized = _clean_relative_prefix(rel)
    if not normalized:
        return False
    for prefix in ignored_paths:
        clean = _clean_relative_prefix(prefix)
        if clean and (normalized == clean or normalized.startswith(clean + "/")):
            return True
    return False


def _parse_verification_commands(
    section: object,
    warnings: list[str],
) -> tuple[ProjectVerificationCommand, ...]:
    if section is None:
        return ()
    if not isinstance(section, dict):
        warnings.append("ignored verification config because it is not an object")
        return ()
    commands = section.get("commands", ())
    if commands in (None, ""):
        return ()
    if not isinstance(commands, list):
        warnings.append("ignored verification.commands because it is not a list")
        return ()
    parsed: list[ProjectVerificationCommand] = []
    for index, item in enumerate(commands[:MAX_VERIFICATION_COMMANDS]):
        if not isinstance(item, dict):
            warnings.append(f"ignored verification.commands[{index}] because it is not an object")
            continue
        command = _single_line(item.get("command"))
        if not command:
            warnings.append(f"ignored verification.commands[{index}] because command is empty")
            continue
        if len(command) > MAX_COMMAND_CHARS:
            warnings.append(f"ignored verification.commands[{index}] because command is too long")
            continue
        cwd = normalize_project_relative_path(item.get("cwd", "."), allow_dot=True)
        if cwd is None:
            warnings.append(f"ignored verification.commands[{index}] because cwd is not project-relative")
            continue
        label = _single_line(item.get("label"))[:MAX_LABEL_CHARS]
        parsed.append(ProjectVerificationCommand(command=command, cwd=cwd, label=label))
    if len(commands) > MAX_VERIFICATION_COMMANDS:
        warnings.append(
            f"ignored {len(commands) - MAX_VERIFICATION_COMMANDS} extra verification command(s)"
        )
    return tuple(parsed)


def _parse_ignored_paths(section: object, warnings: list[str]) -> tuple[str, ...]:
    if section is None:
        return ()
    if not isinstance(section, dict):
        warnings.append("ignored scan config because it is not an object")
        return ()
    values = section.get("ignored_paths", ())
    if values in (None, ""):
        return ()
    if not isinstance(values, list):
        warnings.append("ignored scan.ignored_paths because it is not a list")
        return ()
    parsed: list[str] = []
    for index, value in enumerate(values[:MAX_IGNORED_PATHS]):
        path = normalize_project_relative_path(value, allow_dot=False)
        if path is None:
            warnings.append(f"ignored scan.ignored_paths[{index}] because it is not project-relative")
            continue
        if path not in parsed:
            parsed.append(path)
    if len(values) > MAX_IGNORED_PATHS:
        warnings.append(f"ignored {len(values) - MAX_IGNORED_PATHS} extra ignored path(s)")
    return tuple(parsed)


def _parse_context_budget_hints(
    section: object,
    warnings: list[str],
) -> ProjectContextBudgetHints:
    if section is None:
        return ProjectContextBudgetHints()
    if not isinstance(section, dict):
        warnings.append("ignored context config because it is not an object")
        return ProjectContextBudgetHints()
    budget_hints = section.get("budget_hints", {})
    if not budget_hints:
        return ProjectContextBudgetHints()
    if not isinstance(budget_hints, dict):
        warnings.append("ignored context.budget_hints because it is not an object")
        return ProjectContextBudgetHints()
    project_map_chars = _positive_int(budget_hints.get("project_map_chars"))
    if project_map_chars is None:
        if "project_map_chars" in budget_hints:
            warnings.append("ignored context.budget_hints.project_map_chars because it is not a positive integer")
        return ProjectContextBudgetHints()
    if project_map_chars < MIN_PROJECT_MAP_CHARS:
        warnings.append(
            f"ignored context.budget_hints.project_map_chars because it is below {MIN_PROJECT_MAP_CHARS}"
        )
        return ProjectContextBudgetHints()
    return ProjectContextBudgetHints(project_map_chars=project_map_chars)


def _parse_provider_preferences(
    section: object,
    warnings: list[str],
) -> tuple[ProjectProviderPreference, ...]:
    if section is None:
        return ()
    if not isinstance(section, dict):
        warnings.append("ignored providers config because it is not an object")
        return ()
    preferred = section.get("preferred", {})
    if not preferred:
        return ()
    if not isinstance(preferred, dict):
        warnings.append("ignored providers.preferred because it is not an object")
        return ()
    parsed: list[ProjectProviderPreference] = []
    for raw_mode, raw_provider in preferred.items():
        mode = str(raw_mode or "").strip().lower()
        provider_id = str(raw_provider or "").strip().lower()
        if mode not in PROVIDER_POLICY_MODES:
            warnings.append(f"ignored providers.preferred.{mode or '<empty>'} because mode is unsupported")
            continue
        if provider_id not in PROVIDER_CAPABILITIES:
            warnings.append(f"ignored providers.preferred.{mode} because provider is unsupported")
            continue
        parsed.append(ProjectProviderPreference(mode=mode, provider_id=provider_id))
    return tuple(parsed)


def normalize_project_relative_path(value: object, *, allow_dot: bool = False) -> str | None:
    text = str(value or "").strip().replace("\\", "/")
    if PurePosixPath(text).is_absolute():
        return None
    if ":" in text.split("/", 1)[0]:
        return None
    while text.startswith("./"):
        text = text[2:]
    text = text.strip("/")
    if not text:
        return "." if allow_dot else None
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        return None
    if any(part in {"", "."} for part in path.parts):
        return None
    return path.as_posix()


def _clean_relative_prefix(value: object) -> str:
    path = normalize_project_relative_path(value, allow_dot=False)
    return path or ""


def _single_line(value: object) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return " ".join(text.split())


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number > 0 else None
    return None


def _warning(value: object) -> str:
    text = _single_line(value)
    if len(text) <= MAX_WARNING_CHARS:
        return text
    return text[:MAX_WARNING_CHARS].rstrip() + "..."


__all__ = [
    "MIN_PROJECT_MAP_CHARS",
    "PROJECT_CONFIG_RELATIVE_PATH",
    "ProjectConfig",
    "ProjectConfigLoadResult",
    "ProjectContextBudgetHints",
    "ProjectProviderPreference",
    "ProjectVerificationCommand",
    "load_project_config",
    "normalize_project_relative_path",
    "path_matches_ignored_prefix",
    "render_project_config_warnings",
]
