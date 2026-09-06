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
_PYTEST_OVERRIDE_ARGV_KEYS = frozenset({"addopts"})
_PYTEST_OVERRIDE_PATH_KEYS = frozenset({"cache_dir", "log_file"})
_PYTEST_OVERRIDE_PATH_LIST_KEYS = frozenset({"pythonpath", "testpaths"})
_PYTEST_OVERRIDE_PATH_SHAPED_KEYS = frozenset({
    "doctest_optionflags",
    "norecursedirs",
    "python_files",
    "python_classes",
    "python_functions",
})
_PYTEST_OVERRIDE_NON_PATH_KEYS = frozenset({
    "anyio_mode",
    "asyncio_default_fixture_loop_scope",
    "asyncio_mode",
    "collect_imported_tests",
    "consider_namespace_packages",
    "console_output_style",
    "disable_test_id_escaping_and_forfeit_all_rights_to_community_support",
    "doctest_encoding",
    "empty_parameter_set_mark",
    "enable_assertion_pass_hook",
    "faulthandler_exit_on_timeout",
    "faulthandler_timeout",
    "filterwarnings",
    "junit_suite_name",
    "junit_duration_report",
    "junit_family",
    "junit_logging",
    "junit_log_passing_tests",
    "log_auto_indent",
    "log_cli",
    "log_cli_date_format",
    "log_cli_format",
    "log_cli_level",
    "log_date_format",
    "log_file_date_format",
    "log_file_format",
    "log_file_level",
    "log_format",
    "log_level",
    "markers",
    "minversion",
    "required_plugins",
    "strict",
    "strict_config",
    "strict_markers",
    "strict_parametrization_ids",
    "strict_xfail",
    "truncation_limit_chars",
    "truncation_limit_lines",
    "typeguard-collection-check-strategy",
    "typeguard-debug-instrumentation",
    "typeguard-forward-ref-policy",
    "typeguard-packages",
    "typeguard-typecheck-fail-callback",
    "tmp_path_retention_count",
    "tmp_path_retention_policy",
    "usefixtures",
    "verbosity_assertions",
    "verbosity_subtests",
    "verbosity_test_cases",
    "xfail_strict",
})
_PYTEST_ADDOPTS_MAX_DEPTH = 4
_PYTEST_CODE_LOADING_OPTIONS = frozenset({"--pyargs"})
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
RUN_FORBIDDEN_TOKENS = {"&&", "||", ";", "|", ">", ">>", "<", "$(", "`"}
RUN_ALLOWED_PYTHON_FLAGS = {"-B"}
RUN_ALLOWED_PYTHON_MODULES = {
    "unittest",
    "pytest",
    "py_compile",
    "mypy",
    "ruff",
}
RUN_ALLOWED_NPM_SCRIPTS = {"test", "build", "lint", "check", "typecheck"}
RUN_ALLOWED_MAKE_TARGETS = {"test", "build", "lint", "check", "typecheck"}
RUN_ALLOWED_DENO_TASKS = {"test", "lint", "check"}
RUN_ALLOWED_RUFF_SUBCOMMANDS = {"check", "format"}
RUN_NODE_PACKAGE_MANAGERS = {
    "npm",
    "npm.cmd",
    "npm.exe",
    "pnpm",
    "pnpm.cmd",
    "pnpm.exe",
    "yarn",
    "yarn.cmd",
    "yarn.exe",
}
RUN_BUN_EXECUTABLES = {"bun", "bun.cmd", "bun.exe"}


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
    refs = tuple(_resolved_path_refs(
        root,
        cwd,
        _referenced_path_args(argv, platform=platform),
    ))
    argv_list = list(argv)
    if command_has_forbidden_tokens(argv_list) or not is_allowed_run_command(argv_list):
        raise RunCommandPolicyError(
            "command_not_allowed",
            f"command not allowed: {command}",
        )
    return CanonicalRunCommand(argv=argv, cwd=cwd, referenced_paths=refs)


def command_has_forbidden_tokens(argv: list[str]) -> bool:
    return any(token in arg for arg in argv for token in RUN_FORBIDDEN_TOKENS)


def strip_python_flags(argv: list[str]) -> list[str]:
    args = argv[1:]
    while args and args[0] in RUN_ALLOWED_PYTHON_FLAGS:
        args = args[1:]
    return args


def is_allowed_run_command(argv: list[str]) -> bool:
    if not argv:
        return False
    exe = Path(argv[0]).name.lower()
    if exe in {"python", "python.exe", "py", "py.exe"}:
        args = strip_python_flags(argv)
        if len(args) >= 2 and args[0] == "-m" and args[1] in RUN_ALLOWED_PYTHON_MODULES:
            return _python_module_args_allowed(args[1], args[2:])
        return False
    if exe in {"pytest", "pytest.exe"}:
        return _pytest_args_allowed(argv[1:])
    if exe in {"mypy", "mypy.exe"}:
        return _mypy_args_allowed(argv[1:])
    if exe in {"ruff", "ruff.exe"}:
        return _ruff_args_allowed(argv[1:])
    if exe in RUN_NODE_PACKAGE_MANAGERS:
        return _node_script_allowed(argv)
    if exe in RUN_BUN_EXECUTABLES:
        return _bun_args_allowed(argv)
    if exe in {"deno", "deno.exe"}:
        return _deno_args_allowed(argv[1:])
    if exe in {"go", "go.exe"}:
        return len(argv) >= 2 and argv[1] in {"test", "build", "vet"}
    if exe in {"cargo", "cargo.exe"}:
        return len(argv) >= 2 and argv[1] in {"test", "build", "check"}
    if exe in {"dotnet", "dotnet.exe"}:
        return len(argv) >= 2 and argv[1] in {"test", "build"}
    if exe in {"make", "make.exe", "gmake", "gmake.exe"}:
        return len(argv) >= 2 and all(arg in RUN_ALLOWED_MAKE_TARGETS for arg in argv[1:])
    return False


def is_suite_run_command(argv: list[str]) -> bool:
    if not is_allowed_run_command(argv):
        return False
    exe = Path(argv[0]).name.lower()
    if exe in {"pytest", "pytest.exe", "mypy", "mypy.exe"}:
        return True
    if exe in {"python", "python.exe", "py", "py.exe"}:
        args = strip_python_flags(argv)
        return len(args) >= 2 and args[0] == "-m" and args[1] in {
            "unittest",
            "pytest",
            "mypy",
        }
    if exe in {"go", "go.exe", "cargo", "cargo.exe", "dotnet", "dotnet.exe"}:
        return len(argv) >= 2 and argv[1] in {"test", "build"}
    if exe in {"make", "make.exe", "gmake", "gmake.exe"}:
        return True
    if exe in {"deno", "deno.exe"}:
        return len(argv) >= 2 and argv[1] == "test"
    if exe in RUN_NODE_PACKAGE_MANAGERS or exe in RUN_BUN_EXECUTABLES:
        return len(argv) >= 2 and (
            argv[1] in {"test", "build"}
            or (len(argv) >= 3 and argv[1] == "run" and argv[2] in {"test", "build"})
        )
    return False


def _node_script_allowed(argv: list[str]) -> bool:
    return len(argv) >= 2 and (
        argv[1] in RUN_ALLOWED_NPM_SCRIPTS
        or (len(argv) >= 3 and argv[1] == "run" and argv[2] in RUN_ALLOWED_NPM_SCRIPTS)
    )


def _bun_args_allowed(argv: list[str]) -> bool:
    if len(argv) < 2:
        return False
    if argv[1] == "test":
        return True
    return len(argv) >= 3 and argv[1] == "run" and argv[2] in RUN_ALLOWED_NPM_SCRIPTS


def _ruff_arg_mutates(arg: str) -> bool:
    return (
        arg.startswith("--fix")
        or arg.startswith("--unsafe-fixes")
        or arg.startswith("--add-noqa")
        or arg.startswith("--output-file")
    )


def _ruff_args_allowed(args: list[str]) -> bool:
    if not args or args[0] not in RUN_ALLOWED_RUFF_SUBCOMMANDS:
        return False
    rest = args[1:]
    if args[0] == "format":
        return "--check" in rest
    return not any(_ruff_arg_mutates(arg) for arg in rest)


def _mypy_args_allowed(args: list[str]) -> bool:
    return not any(arg.startswith("--install-types") for arg in args)


def _deno_args_allowed(args: list[str]) -> bool:
    if not args:
        return False
    if args[0] == "fmt":
        return "--check" in args[1:]
    return args[0] in RUN_ALLOWED_DENO_TASKS


def _python_module_args_allowed(module: str, args: list[str]) -> bool:
    if module == "ruff":
        return _ruff_args_allowed(args)
    if module == "mypy":
        return _mypy_args_allowed(args)
    if module == "pytest":
        return _pytest_args_allowed(args)
    return True


def _pytest_args_allowed(args: Sequence[str], *, addopts_depth: int = 0) -> bool:
    values = list(args)
    index = 0
    while index < len(values):
        arg = values[index]
        if arg == "--":
            break
        override_token = _pytest_override_token(values, index)
        if override_token is not None:
            token, consumed = override_token
            if not _pytest_override_args_allowed(token, addopts_depth=addopts_depth):
                return False
            index += consumed
        elif _pytest_code_loading_option(arg):
            return False
        index += 1
    return True


def _pytest_override_args_allowed(token: str, *, addopts_depth: int) -> bool:
    key, sep, raw = token.partition("=")
    if not sep or key.strip().lower() != "addopts":
        return True
    if addopts_depth >= _PYTEST_ADDOPTS_MAX_DEPTH:
        return False
    try:
        nested = _split_pytest_override_value(raw, platform=None)
    except RunCommandPolicyError:
        return False
    return _pytest_args_allowed(nested, addopts_depth=addopts_depth + 1)


def _pytest_code_loading_option(arg: str) -> bool:
    option, _sep, _value = str(arg or "").partition("=")
    if option in _PYTEST_CODE_LOADING_OPTIONS:
        return True
    return option == "-p" or (option.startswith("-p") and not option.startswith("--") and len(option) > 2)


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


def _referenced_path_args(
    argv: Sequence[str],
    *,
    platform: str | None,
) -> tuple[str, ...]:
    if not argv:
        return ()
    exe = _executable_name(argv[0])
    if exe in {"python", "python.exe", "py", "py.exe"}:
        return _python_path_args(argv[1:], platform=platform)
    if exe in {"pytest", "pytest.exe"}:
        return _pytest_path_args(argv[1:], platform=platform)
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


def _python_path_args(
    args: Sequence[str],
    *,
    platform: str | None,
) -> tuple[str, ...]:
    rest = list(args)
    while rest and rest[0] == "-B":
        rest = rest[1:]
    if not rest:
        return ()
    if len(rest) >= 2 and rest[0] == "-m":
        module = rest[1]
        module_args = rest[2:]
        if module == "pytest":
            return _pytest_path_args(module_args, platform=platform)
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
    return (
        _path_arg(rest[0]),
        *_option_path_args(
            rest[1:],
            path_value_options=frozenset(),
            positional_requires_path_shape=True,
        ),
    )


def _pytest_path_args(
    args: Sequence[str],
    *,
    platform: str | None,
    addopts_depth: int = 0,
) -> tuple[str, ...]:
    refs: list[str] = []
    rest: list[str] = []
    values = list(args)
    index = 0
    while index < len(values):
        arg = values[index]
        if arg == "--":
            rest.extend(values[index:])
            break
        override_token = _pytest_override_token(values, index)
        if override_token is not None:
            token, consumed = override_token
            refs.extend(_pytest_override_path_args(
                token,
                platform=platform,
                addopts_depth=addopts_depth,
            ))
            index += consumed
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


def _pytest_override_token(values: Sequence[str], index: int) -> tuple[str, int] | None:
    arg = values[index]
    if arg in _PYTEST_OVERRIDE_OPTIONS:
        return (values[index + 1] if index + 1 < len(values) else "", 1)
    if arg.startswith("--override-ini="):
        return arg.partition("=")[2], 0
    if arg.startswith("-o="):
        return arg[3:], 0
    if arg.startswith("-o") and not arg.startswith("--") and len(arg) > 2:
        return arg[2:], 0
    return None


def _pytest_override_path_args(
    token: str,
    *,
    platform: str | None,
    addopts_depth: int,
) -> tuple[str, ...]:
    """Return filesystem operands carried by one pytest ini override."""

    key, sep, raw = token.partition("=")
    normalized = key.strip().lower()
    if not sep or not normalized:
        raise RunCommandPolicyError(
            "invalid_pytest_override",
            "pytest ini override must use option=value syntax",
        )
    value = raw.strip()
    if not value:
        return ()
    if normalized in _PYTEST_OVERRIDE_ARGV_KEYS:
        if addopts_depth >= _PYTEST_ADDOPTS_MAX_DEPTH:
            raise RunCommandPolicyError(
                "invalid_pytest_override",
                "pytest addopts nesting is too deep",
            )
        return _pytest_path_args(
            _split_pytest_override_value(value, platform=platform),
            platform=platform,
            addopts_depth=addopts_depth + 1,
        )
    if normalized in _PYTEST_OVERRIDE_PATH_KEYS:
        return (_path_arg(value),)
    if normalized in _PYTEST_OVERRIDE_PATH_LIST_KEYS:
        return tuple(
            _path_arg(item)
            for item in _split_pytest_override_value(value, platform=platform)
            if item
        )
    if normalized in _PYTEST_OVERRIDE_PATH_SHAPED_KEYS:
        return tuple(
            _path_arg(item)
            for item in _split_pytest_override_value(value, platform=platform)
            if _looks_like_path(item)
        )
    if normalized in _PYTEST_OVERRIDE_NON_PATH_KEYS:
        return ()
    raise RunCommandPolicyError(
        "unsupported_pytest_override",
        "pytest ini override is not allowed by run-command policy",
    )


def _split_pytest_override_value(
    value: str,
    *,
    platform: str | None,
) -> tuple[str, ...]:
    try:
        return tuple(split_run_command(value, platform=platform))
    except ValueError as exc:
        raise RunCommandPolicyError(
            "invalid_pytest_override",
            "pytest ini override could not be tokenized",
        ) from exc


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
        text = text[1:]
    while text.startswith("-") and "=" in text:
        _option, _sep, value = text.partition("=")
        if not value or value == text:
            break
        text = value.strip()
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
