"""User-facing risk descriptions for approved shell commands."""

from __future__ import annotations

from dataclasses import dataclass

from codey.command_line import split_run_command


@dataclass(frozen=True)
class ShellRisk:
    label: str
    title: str
    detail: str
    post_approval_instructions: str


GENERIC_RISK = ShellRisk(
    label="generic",
    title="Shell command",
    detail="May read or change files, start processes, or use local credentials.",
    post_approval_instructions=(
        "Post-approval checklist:\n"
        "- Inspect the shell exit code and output before claiming success.\n"
        "- Do not claim command, build, test, install, or publish success unless "
        "it appears in the shell output or a later local tool result."
    ),
)


def classify_shell_risk(command: str) -> ShellRisk:
    """Classify a shell command for explanation only, not authorization."""

    argv = _argv(command)
    text = " ".join(argv)
    if _is_dependency_install(argv):
        return ShellRisk(
            label="dependency_install",
            title="Dependency install",
            detail=(
                "May download packages, write dependency folders or lockfiles, "
                "and run install scripts."
            ),
            post_approval_instructions=(
                "Post-approval checklist:\n"
                "- Inspect the shell exit code and output before claiming success.\n"
                "- If dependencies were installed, inspect relevant manifest or "
                "lockfile changes when useful.\n"
                "- If a trusted local check is available, run it before done.\n"
                "- Do not claim install, build, or test success unless it appears "
                "in tool output."
            ),
        )
    if _is_system_install(argv):
        return ShellRisk(
            label="system_install",
            title="System install",
            detail=(
                "May install software outside this project and may require PATH "
                "or terminal refresh afterward."
            ),
            post_approval_instructions=(
                "Post-approval checklist:\n"
                "- Inspect the shell exit code and output before claiming success.\n"
                "- If the tool still appears missing, mention that PATH or the "
                "terminal session may need to be refreshed.\n"
                "- Do not claim the project is verified until a relevant local "
                "check has passed."
            ),
        )
    if _is_publish(argv):
        return ShellRisk(
            label="publish",
            title="Publish or push",
            detail=(
                "May send commits, packages, releases, or deployment state to an "
                "external service."
            ),
            post_approval_instructions=(
                "Post-approval checklist:\n"
                "- Inspect the shell exit code and output before claiming success.\n"
                "- Confirm the diff, status, or publish output before saying "
                "anything was pushed or released.\n"
                "- Do not claim tests passed unless a trusted local check appears "
                "in tool output."
            ),
        )
    if _is_external_source(argv, text):
        return ShellRisk(
            label="external_source",
            title="External source",
            detail=(
                "May download or update code from outside the selected project."
            ),
            post_approval_instructions=(
                "Post-approval checklist:\n"
                "- Inspect the shell exit code and output before claiming success.\n"
                "- For newly downloaded code, read README or manifest files before "
                "running project code.\n"
                "- Do not assume external source is safe or complete without local "
                "inspection."
            ),
        )
    if _is_dev_server(argv, text):
        return ShellRisk(
            label="dev_server",
            title="Dev server",
            detail=(
                "May start a long-running local process; Codey does not manage "
                "background dev servers in this flow."
            ),
            post_approval_instructions=(
                "Post-approval checklist:\n"
                "- Inspect the shell exit code and output before claiming success.\n"
                "- Codey is not managing a background dev server here; a synchronous "
                "shell command may time out for long-running servers.\n"
                "- If the project changed, run a trusted local check before done "
                "when available."
            ),
        )
    return GENERIC_RISK


def _argv(command: str) -> list[str]:
    raw = _split(command)
    unwrapped = _unwrap_shell(raw)
    return [_normalize_token(part) for part in unwrapped if _normalize_token(part)]


def _split(command: str) -> list[str]:
    # Same tokenizer as execution, so the approval card always describes the
    # command that will actually run. If even that fails, fall back to a raw
    # whitespace split: risk classification is display-only and must not
    # invent structure the parser could not see.
    try:
        return split_run_command(command or "")
    except ValueError:
        return str(command or "").split()


def _unwrap_shell(argv: list[str]) -> list[str]:
    normalized = [_normalize_token(part) for part in argv]
    if not normalized:
        return []
    if normalized[0] in {"powershell", "pwsh"}:
        for index, token in enumerate(normalized[1:], start=1):
            if token in {"-command", "-c"} and index + 1 < len(argv):
                command = " ".join(_strip_quotes(part) for part in argv[index + 1:])
                return _split(command)
    if normalized[0] == "cmd":
        switches = [_strip_quotes(part).lower() for part in argv[1:]]
        for index, token in enumerate(switches, start=1):
            if token in {"/c", "/k"} and index + 1 < len(argv):
                command = " ".join(_strip_quotes(part) for part in argv[index + 1:])
                return _split(command)
    return argv


def _normalize_token(token: str) -> str:
    text = _strip_quotes(token).replace("\\", "/").strip()
    if not text:
        return ""
    name = text.rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def _strip_quotes(value: str) -> str:
    return str(value or "").strip().strip("\"'")


def _starts_with(argv: list[str], *prefix: str) -> bool:
    return len(argv) >= len(prefix) and tuple(argv[:len(prefix)]) == prefix


def _is_dependency_install(argv: list[str]) -> bool:
    return (
        _node_package_install(argv)
        or _pip_install(argv)
        or _starts_with(argv, "poetry", "install")
        or _starts_with(argv, "uv", "sync")
        or _starts_with(argv, "uv", "pip", "install")
        or _starts_with(argv, "uv", "add")
        or _starts_with(argv, "go", "get")
        or _starts_with(argv, "cargo", "add")
        or _starts_with(argv, "deno", "install")
        or (len(argv) >= 2 and argv[0] == "npx")
    )


def _node_package_install(argv: list[str]) -> bool:
    return len(argv) >= 2 and argv[0] in {"npm", "pnpm", "yarn"} and argv[1] in {
        "install",
        "i",
        "ci",
        "add",
    }


def _pip_install(argv: list[str]) -> bool:
    if len(argv) >= 2 and argv[0] in {"pip", "pip3"} and argv[1] == "install":
        return True
    if not argv or not (argv[0] == "py" or argv[0].startswith("python")):
        return False
    index = 1
    while index < len(argv) and argv[index].startswith("-") and argv[index] != "-m":
        index += 1
    return argv[index:index + 3] == ["-m", "pip", "install"]


def _is_system_install(argv: list[str]) -> bool:
    return (
        _starts_with(argv, "winget", "install")
        or _starts_with(argv, "choco", "install")
        or _starts_with(argv, "scoop", "install")
        or _starts_with(argv, "apt", "install")
        or _starts_with(argv, "apt-get", "install")
    )


def _is_external_source(argv: list[str], text: str) -> bool:
    return (
        _starts_with(argv, "git", "clone")
        or _starts_with(argv, "git", "pull")
        or _starts_with(argv, "gh", "repo", "clone")
        or _starts_with(argv, "curl")
        or _starts_with(argv, "wget")
        or _starts_with(argv, "invoke-webrequest")
        or _starts_with(argv, "iwr")
        or _starts_with(argv, "invoke-restmethod")
        or _starts_with(argv, "irm")
        or text.startswith("powershell invoke-webrequest")
        or text.startswith("pwsh invoke-webrequest")
    )


def _is_publish(argv: list[str]) -> bool:
    return (
        _starts_with(argv, "git", "push")
        or _starts_with(argv, "npm", "publish")
        or _starts_with(argv, "twine", "upload")
        or _starts_with(argv, "gh", "release")
    )


def _is_dev_server(argv: list[str], text: str) -> bool:
    return (
        _starts_with(argv, "npm", "run", "dev")
        or _starts_with(argv, "npm", "start")
        or _starts_with(argv, "pnpm", "dev")
        or _starts_with(argv, "yarn", "dev")
        or _starts_with(argv, "vite")
        or _starts_with(argv, "next", "dev")
        or _starts_with(argv, "uvicorn")
        or _starts_with(argv, "flask", "run")
        or text.startswith("python -m flask run")
    )
