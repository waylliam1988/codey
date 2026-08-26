"""Canonical run-command semantics for project-scoped verification.

The command tokenizer guarantees that policy and execution see the same argv.
This module adds the next layer: commands that accept filesystem arguments must
have those path references resolved against the same project cwd before a run
is allowed to execute.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from codey.policies.command_line import split_run_command


_WINDOWS_DRIVE_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_PATH_SUFFIXES = frozenset({
    ".cfg",
    ".ini",
    ".json",
    ".lock",
    ".py",
    ".pyi",
    ".toml",
    ".xml",
    ".yaml",
    ".yml",
})

_PYTEST_OVERRIDE_OPTIONS = frozenset({"-o", "--override-ini"})
_PYTEST_OVERRIDE_PATH_KEYS = frozenset({"cache_dir"})
_PYTEST_PATH_VALUE_OPTIONS = frozenset({
    "-c",
    "--basetemp",
    "--confcutdir",
    "--junit-xml",
    "--junitxml",
    "--rootdir",
})
_PYTEST_NON_PATH_VALUE_OPTIONS = frozenset({
    "-k",
    "-m",
    "--capture",
    "--color",
    "--import-mode",
    "--log-cli-level",
    "--log-file-level",
    "--maxfail",
    "--tb",
    "--verbosity",
})
_MYPY_PATH_VALUE_OPTIONS = frozenset({
    "--cache-dir",
    "--config-file",
    "--custom-typeshed-dir",
    "--exclude-gitignore",
})
_MYPY_NON_PATH_VALUE_OPTIONS = frozenset({
    "--cache-fine-grained",
    "--follow-imports",
    "--junit-format",
    "--no-site-packages",
    "--package",
    "--python-version",
    "--show-error-codes",
})
_RUFF_PATH_VALUE_OPTIONS = frozenset({
    "--config",
    "--extend",
    "--stdin-filename",
})
_RUFF_NON_PATH_VALUE_OPTIONS = frozenset({
    "--exit-non-zero-on-fix",
    "--output-format",
    "--select",
    "--target-version",
})
_DENO_PATH_VALUE_OPTIONS = frozenset({
    "--cert",
    "--config",
    "--import-map",
    "--lock",
})
_GO_PATH_VALUE_OPTIONS = frozenset({
    "-coverprofile",
    "-o",
})
_CARGO_PATH_VALUE_OPTIONS = frozenset({
    "--manifest-path",
    "--target-dir",
})
_DOTNET_PATH_VALUE_OPTIONS = frozenset({
    "--artifacts-path",
    "--results-directory",
    "--settings",
})


@dataclass(frozen=True)
class RunCommandPathRef:
    raw: str
    resolved: Path


@dataclass(frozen=True)
class CanonicalRunCommand:
    argv: tuple[str, ...]
    cwd: Path
    referenced_paths: tuple[RunCommandPathRef, ...] = ()


class RunCommandPolicyError(ValueError):
    def __init__(self, reason_code: str, display: str) -> None:
        super().__init__(display)
        self.reason_code = reason_code
        self.display = display


def canonical_run_command(
    project: str | Path,
    rel: str,
    command: str,
    *,
    platform: str | None = None,
) -> CanonicalRunCommand:
    """Return a canonical run command or fail closed with a policy error."""

    root = _project_root(project)
    try:
        argv = tuple(split_run_command(command, platform=platform))
    except ValueError as exc:
        raise RunCommandPolicyError(
            "invalid_command",
            f"invalid command: {exc}",
        ) from exc
    cwd = _resolve_inside_project(root, root, rel or ".", subject="path")
    refs = tuple(_resolved_path_refs(root, cwd, _referenced_path_args(argv)))
    return CanonicalRunCommand(argv=argv, cwd=cwd, referenced_paths=refs)


def _project_root(project: str | Path) -> Path:
    if not str(project or "").strip():
        raise RunCommandPolicyError(
            "project_required",
            "project required for local workspace action",
        )
    try:
        return Path(project).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        raise RunCommandPolicyError(
            "path_resolution_failed",
            "project path could not be resolved",
        ) from None


def _resolve_inside_project(root: Path, cwd: Path, raw: str, *, subject: str) -> Path:
    try:
        candidate = _resolve_path(root, cwd, raw)
    except (OSError, RuntimeError, ValueError):
        raise RunCommandPolicyError(
            "path_resolution_failed",
            "run command path could not be resolved",
        ) from None
    if root not in candidate.parents and candidate != root:
        # The raw operand never enters the display: audit records must not
        # retain unverified filesystem paths sourced from a command string.
        if subject == "command_path":
            raise RunCommandPolicyError(
                "command_path_escape",
                "run command references a path outside the project root",
            )
        raise RunCommandPolicyError(
            "workspace_escape",
            "path escapes the project root",
        )
    return candidate


def _resolve_path(root: Path, cwd: Path, raw: str) -> Path:
    text = _command_path_text(raw)
    if not text:
        return cwd
    if _is_windows_absolute(text) and not Path(text).is_absolute():
        raise ValueError(f"windows absolute path is not valid on this platform: {raw}")
    path = Path(text).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (cwd / path).resolve()


def _resolved_path_refs(
    root: Path,
    cwd: Path,
    raw_paths: Iterable[str],
) -> Iterable[RunCommandPathRef]:
    for raw in raw_paths:
        resolved = _resolve_inside_project(root, cwd, raw, subject="command_path")
        yield RunCommandPathRef(raw=raw, resolved=resolved)


def _referenced_path_args(argv: Sequence[str]) -> tuple[str, ...]:
    if not argv:
        return ()
    exe = _executable_name(argv[0])
    if exe in {"python", "python.exe", "py", "py.exe"}:
        return _python_path_args(argv[1:])
    if exe in {"pytest", "pytest.exe"}:
        return _pytest_path_args(argv[1:])
    if exe in {"mypy", "mypy.exe"}:
        return _option_path_args(
            argv[1:],
            path_value_options=_MYPY_PATH_VALUE_OPTIONS,
            non_path_value_options=_MYPY_NON_PATH_VALUE_OPTIONS,
        )
    if exe in {"ruff", "ruff.exe"}:
        return _option_path_args(
            argv[2:] if len(argv) >= 2 else (),
            path_value_options=_RUFF_PATH_VALUE_OPTIONS,
            non_path_value_options=_RUFF_NON_PATH_VALUE_OPTIONS,
        )
    if exe in {"deno", "deno.exe"}:
        return _option_path_args(
            argv[2:] if len(argv) >= 2 else (),
            path_value_options=_DENO_PATH_VALUE_OPTIONS,
            positional_requires_path_shape=True,
        )
    if exe in {"go", "go.exe"}:
        return _option_path_args(
            argv[2:] if len(argv) >= 2 else (),
            path_value_options=_GO_PATH_VALUE_OPTIONS,
            positional_requires_path_shape=True,
        )
    if exe in {"cargo", "cargo.exe"}:
        return _option_path_args(
            argv[2:] if len(argv) >= 2 else (),
            path_value_options=_CARGO_PATH_VALUE_OPTIONS,
            positional_requires_path_shape=True,
        )
    if exe in {"dotnet", "dotnet.exe"}:
        return _option_path_args(
            argv[2:] if len(argv) >= 2 else (),
            path_value_options=_DOTNET_PATH_VALUE_OPTIONS,
            positional_requires_path_shape=True,
        )
    if exe in {
        "npm",
        "npm.cmd",
        "npm.exe",
        "pnpm",
        "pnpm.cmd",
        "pnpm.exe",
        "yarn",
        "yarn.cmd",
        "yarn.exe",
        "bun",
        "bun.cmd",
        "bun.exe",
    }:
        return _option_path_args(
            _package_script_args(argv),
            path_value_options=frozenset(),
            positional_requires_path_shape=True,
        )
    return ()


def _python_path_args(args: Sequence[str]) -> tuple[str, ...]:
    rest = list(args)
    while rest and rest[0] == "-B":
        rest = rest[1:]
    if not rest:
        return ()
    if len(rest) >= 2 and rest[0] == "-m":
        module = rest[1]
        module_args = rest[2:]
        if module == "pytest":
            return _pytest_path_args(module_args)
        if module == "py_compile":
            return _option_path_args(module_args, path_value_options=frozenset())
        if module == "mypy":
            return _option_path_args(
                module_args,
                path_value_options=_MYPY_PATH_VALUE_OPTIONS,
                non_path_value_options=_MYPY_NON_PATH_VALUE_OPTIONS,
            )
        if module == "ruff":
            return _option_path_args(
                module_args[1:] if module_args else (),
                path_value_options=_RUFF_PATH_VALUE_OPTIONS,
                non_path_value_options=_RUFF_NON_PATH_VALUE_OPTIONS,
            )
        if module == "unittest":
            return _unittest_path_args(module_args)
        return ()
    return (_path_arg(rest[0]),)


def _pytest_path_args(args: Sequence[str]) -> tuple[str, ...]:
    refs: list[str] = []
    rest: list[str] = []
    values = list(args)
    index = 0
    while index < len(values):
        arg = values[index]
        if arg == "--":
            rest.extend(values[index:])
            break
        name, has_inline, inline = arg.partition("=")
        if arg.startswith("-") and name in _PYTEST_OVERRIDE_OPTIONS:
            token = inline
            if not has_inline and index + 1 < len(values):
                token = values[index + 1]
                index += 1
            value = _pytest_override_path_arg(token)
            if value is not None:
                refs.append(_path_arg(value))
        else:
            rest.append(arg)
        index += 1
    return (
        *refs,
        *_option_path_args(
            rest,
            path_value_options=_PYTEST_PATH_VALUE_OPTIONS,
            non_path_value_options=_PYTEST_NON_PATH_VALUE_OPTIONS,
        ),
    )


def _pytest_override_path_arg(token: str) -> str | None:
    """Return the ini value when an -o/--override-ini key carries a path."""

    key, sep, raw = token.partition("=")
    if sep and key.strip().lower() in _PYTEST_OVERRIDE_PATH_KEYS:
        value = raw.strip()
        return value or None
    return None


def _unittest_path_args(args: Sequence[str]) -> tuple[str, ...]:
    if not args:
        return ()
    if args[0] != "discover":
        return tuple(
            _path_arg(arg)
            for arg in args
            if not arg.startswith("-") and _looks_like_path(arg)
        )
    return _option_path_args(
        args[1:],
        path_value_options=frozenset({"-s", "--start-directory", "-t", "--top-level-directory"}),
        non_path_value_options=frozenset({"-p", "--pattern"}),
    )


def _option_path_args(
    args: Sequence[str],
    *,
    path_value_options: frozenset[str],
    non_path_value_options: frozenset[str] = frozenset(),
    positional_requires_path_shape: bool = False,
) -> tuple[str, ...]:
    refs: list[str] = []
    index = 0
    values = list(args)
    while index < len(values):
        arg = values[index]
        if not arg:
            index += 1
            continue
        if arg == "--":
            refs.extend(
                _path_arg(item)
                for item in values[index + 1 :]
                if _should_collect_positional(item, positional_requires_path_shape)
            )
            break
        option, has_value, value = arg.partition("=")
        compact_value = _compact_option_value(arg, path_value_options)
        if compact_value is not None:
            refs.append(_path_arg(compact_value))
        elif arg.startswith("-"):
            if has_value and option in path_value_options:
                refs.append(_path_arg(value))
            elif has_value and _looks_like_path(value):
                refs.append(_path_arg(value))
            elif option in path_value_options:
                if index + 1 < len(values):
                    refs.append(_path_arg(values[index + 1]))
                    index += 1
            elif option in non_path_value_options:
                if index + 1 < len(values):
                    index += 1
        elif _should_collect_positional(arg, positional_requires_path_shape):
            refs.append(_path_arg(arg))
        index += 1
    return tuple(ref for ref in refs if ref)


def _should_collect_positional(arg: str, requires_path_shape: bool) -> bool:
    return _looks_like_path(arg) if requires_path_shape else True


def _compact_option_value(
    arg: str,
    path_value_options: frozenset[str],
) -> str | None:
    for option in sorted(path_value_options, key=len, reverse=True):
        if option.startswith("--") or len(option) != 2:
            continue
        if arg.startswith(option) and len(arg) > len(option):
            return arg[len(option) :]
    return None


def _package_script_args(argv: Sequence[str]) -> tuple[str, ...]:
    exe = _executable_name(argv[0]) if argv else ""
    if exe in {"bun", "bun.cmd", "bun.exe"} and len(argv) >= 2 and argv[1] == "test":
        return tuple(argv[2:])
    if len(argv) >= 3 and argv[1] == "run":
        return tuple(argv[3:])
    if len(argv) >= 2:
        return tuple(argv[2:])
    return ()


def _path_arg(raw: str) -> str:
    text = str(raw or "").strip()
    if text.startswith("@") and len(text) > 1:
        return text[1:]
    return text


def _command_path_text(raw: str) -> str:
    text = _path_arg(raw)
    if not text or text == "-":
        return ""
    if "::" in text:
        text = text.split("::", 1)[0]
    return text


def _looks_like_path(raw: str) -> bool:
    text = _command_path_text(raw)
    if not text:
        return False
    if "://" in text:
        return False
    if text in {".", ".."}:
        return True
    if text.startswith(("./", "../", ".\\", "..\\", "/", "\\", "~")):
        return True
    if _is_windows_absolute(text):
        return True
    if "/" in text or "\\" in text:
        return True
    return Path(text.replace("\\", "/")).suffix.lower() in _PATH_SUFFIXES


def _is_windows_absolute(text: str) -> bool:
    return bool(_WINDOWS_DRIVE_ABSOLUTE_RE.match(text))


def _executable_name(value: str) -> str:
    return str(value or "").replace("\\", "/").rsplit("/", 1)[-1].lower()


__all__ = [
    "CanonicalRunCommand",
    "RunCommandPathRef",
    "RunCommandPolicyError",
    "canonical_run_command",
]
