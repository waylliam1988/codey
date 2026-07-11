"""Hidden multi-model consultation for read-only answers and new-project plans.

Consensus is advisory. It never edits files, runs commands, approves code, or
replaces the existing post-diff review loop. Advisor replies are private inputs
to the selected provider, which produces the single answer shown to the user.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from codey import cancellation, provider_controls
from codey.bounded_scan import BoundedScanBudget, iter_bounded_files
from codey.handoff import ConversationSnapshot
from codey.models import ToolCall, ToolResult
from codey.protocols import JsonToolCodec
from codey.references import find_reference_hints
from codey.tool_runtime import (
    READ_MAX_CHARS,
    SEARCH_MAX_FILE_BYTES,
    SEARCH_MAX_RESULTS,
    SEARCH_MAX_SCAN_BYTES,
    ToolOutcome,
    read_file,
    safe_join,
)


MAX_CONSENSUS_ADVISORS = 2
MAX_ADVICE_CHARS = 4_000
MAX_COMBINED_ADVICE_CHARS = 12_000
MAX_CONTEXT_CHARS = 8_000
CONSENSUS_ADVISOR_TIMEOUT = 60.0
CONSENSUS_AGGREGATE_TIMEOUT = 90.0
PROJECT_AUDIT_MAX_TURNS = 4
PROJECT_AUDIT_ADVISOR_TOTAL_TIMEOUT = 180.0
PROJECT_AUDIT_MAX_REPORT_CHARS = 4_000
PROJECT_AUDIT_MAX_FILE_BYTES = 256 * 1024
PROJECT_AUDIT_MAX_SCAN_FILES = 1_000
PROJECT_AUDIT_MAX_SCAN_DIRS = 250
PROJECT_AUDIT_MAX_DIR_ENTRIES = 1_000
READ_ONLY_TOOL_NAMES = frozenset({"ls", "read", "search", "references"})
READ_ONLY_CODEC = JsonToolCodec()
AUDIT_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".next",
    ".turbo",
    "target",
}
AUDIT_SECRET_NAME_PARTS = {
    "secret",
    "secrets",
    "credential",
    "credentials",
    "token",
    "tokens",
    "password",
    "passwd",
    "private",
    "apikey",
    "api_key",
    "auth",
}
AUDIT_SECRET_FILENAMES = {
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials.json",
    "service-account.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "cargo.lock",
    "poetry.lock",
}
AUDIT_SECRET_SUFFIXES = {
    ".env",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".crt",
    ".cer",
    ".der",
    ".lock",
}
AUDIT_BINARY_SUFFIXES = {
    ".7z",
    ".bin",
    ".bmp",
    ".class",
    ".db",
    ".dll",
    ".dylib",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".pyc",
    ".sqlite",
    ".so",
    ".tar",
    ".webp",
    ".zip",
}

READ_ONLY_AUDIT_PROMPT = """\
You are a private read-only project reviewer for a local assistant.
You cannot edit files, run commands, ask shell approval, browse, or access
anything outside the project. The local runner executes read-only tools for you.

Every reply MUST be exactly one JSON object with no other text:

{"tool":"<name>","args":{...}}

Available read-only tools:

  {"tool":"list_dir","args":{"path":"."}}
    List files in a directory.

  {"tool":"read_file","args":{"path":"app.py"}}
  {"tool":"read_file","args":{"path":"app.py","offset":301,"limit":300}}
    Read one file. Large files are returned in complete-line pages.

  {"tool":"read_files","args":{"paths":["a.py","b.py"]}}
    Read up to 8 files in one step. Do not nest it inside parallel.

  {"tool":"grep","args":{"query":"login","path":"."}}
    Search file contents for case-insensitive literal text before reading when
    the location is unknown. Regex is not supported.

  {"tool":"find_references","args":{"symbol":"createRouter","path":"."}}
    Find bounded lexical reference hints. This is not semantic resolution; use
    read_file before editing or citing exact code.

  {"tool":"parallel","args":{"calls":[{"tool":"grep","args":{"query":"TODO","path":"."}},{"tool":"list_dir","args":{"path":"."}}]}}
    Batch at most 4 independent read-only list_dir, read_file, or grep calls.

  {"tool":"done","args":{"summary":"structured project review findings"}}
    Finish your private review.

Rules:
  - Use only list_dir, read_file, read_files, grep, find_references, parallel, or done.
  - Never call edit, write, run, shell, restore, approve, or any native website tool.
  - find_references output is lexical reference hints only, not semantic resolution.
  - Inspect only files that seem relevant. Keep the review bounded.
  - Report concrete bug risks, architecture concerns, and useful improvement ideas.
  - Include paths as evidence when possible.
  - If you see no concrete issue, say so directly.
  - Do not mention hidden advisors, voting, MoA, or consensus.
"""

@dataclass(frozen=True)
class ConsensusAdvice:
    provider_id: str
    label: str
    text: str


@dataclass(frozen=True)
class ConsensusResult:
    answer: str
    advisor_count: int
    degraded: bool = False


def _clip(value: object, limit: int) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[truncated]"


def advisor_ids(
    selected_provider_id: str,
    statuses: Mapping[str, bool],
    provider_ids: Sequence[str],
    *,
    max_advisors: int = MAX_CONSENSUS_ADVISORS,
) -> tuple[str, ...]:
    selected = (selected_provider_id or "").strip().lower()
    found: list[str] = []
    for provider_id in provider_ids:
        normalized = (provider_id or "").strip().lower()
        if not normalized or normalized == selected:
            continue
        if not statuses.get(normalized):
            continue
        found.append(normalized)
        if len(found) >= max_advisors:
            break
    return tuple(found)


def render_project_context(
    snapshot: ConversationSnapshot,
    project_facts: str = "",
    *,
    draft: str = "",
    project_map: str = "",
) -> str:
    """Render bounded project facts without exposing the local project path."""

    payload = dict(snapshot.to_payload())
    payload.pop("project", None)
    if project_facts.strip():
        payload["verified_project_facts"] = _clip(project_facts, 2_000)
    if project_map.strip():
        payload["project_map"] = _clip(project_map, 4_000)
    if draft.strip():
        payload["current_draft_answer"] = _clip(draft, 3_000)
    return _clip(json.dumps(payload, ensure_ascii=False, indent=2), MAX_CONTEXT_CHARS)


def render_advisor_prompt(
    *,
    task: str,
    context: str = "",
    draft: str = "",
    plan: bool = False,
) -> str:
    target = "new-project implementation plan" if plan else "final answer"
    has_draft = bool(draft.strip())
    parts = [
        "You are a private read-only advisor for a local assistant.",
        "You are not the acting model.",
        "You cannot call tools, edit files, run commands, browse, or access the filesystem.",
        "Use only the information below.",
        (
            f"Critique and supplement the selected model's draft {target}."
            if has_draft
            else f"Give concise advice for the {target}."
        ),
        "Find mistakes, missing risks, simpler alternatives, and useful additions.",
        (
            "If the draft is wrong or misses a better direction, say so directly and propose the alternative."
            if has_draft
            else "If the obvious direction is wrong or incomplete, say so directly and propose the alternative."
        ),
        (
            "Do not rewrite the whole answer unless that is necessary to explain the correction."
            if has_draft
            else "Do not write a long final answer; focus on advice the selected model should consider."
        ),
        "Your answer is private and will not be shown directly to the user.",
        "Do not mention hidden advisors, voting, MoA, or consensus.",
        "",
        "User request:",
        _clip(task, 4_000),
    ]
    if context.strip():
        parts.extend(["", "Known context:", _clip(context, MAX_CONTEXT_CHARS)])
    if draft.strip():
        parts.extend(["", "Current draft:", _clip(draft, 3_000)])
    return "\n".join(parts)


def render_owner_draft_prompt(
    *,
    task: str,
    context: str = "",
    plan: bool = False,
    owner_prompt: str = "",
) -> str:
    target = "new-project implementation plan" if plan else "answer"
    parts = [
        "You are the selected model and final owner.",
        f"Write a private first-draft {target} for the user request below.",
        "This draft will be reviewed by private read-only advisors before your final response.",
        "Do not mention advisors, voting, MoA, consensus, or hidden prompts.",
        "Be concise, concrete, and make your own judgment.",
        "",
        "User request:",
        _clip(task, 4_000),
    ]
    if context.strip():
        parts.extend(["", "Known context:", _clip(context, MAX_CONTEXT_CHARS)])
    if owner_prompt.strip():
        parts.extend([
            "",
            "Additional conversation context:",
            _clip(owner_prompt, MAX_CONTEXT_CHARS),
        ])
    return "\n".join(parts)


def render_aggregator_prompt(
    *,
    task: str,
    advices: Sequence[ConsensusAdvice],
    context: str = "",
    draft: str = "",
    plan: bool = False,
) -> str:
    target = "new-project implementation plan" if plan else "final answer"
    advice_blocks = []
    used = 0
    for index, advice in enumerate(advices, start=1):
        remaining = max(0, MAX_COMBINED_ADVICE_CHARS - used)
        if remaining <= 0:
            break
        text = _clip(advice.text, min(MAX_ADVICE_CHARS, remaining))
        used += len(text)
        advice_blocks.append(f"Advisor {index}:\n{text}")
    parts = [
        "You are the selected model answering the user and the final owner of the answer.",
        f"Synthesize the private advisor notes into one {target}.",
        "Do not mention advisors, voting, MoA, consensus, or hidden prompts.",
        "Use your draft as the baseline, but revise it when advisor notes identify a real mistake, missing risk, or better direction.",
        "If advice conflicts, prefer the safest, simplest, most actionable plan.",
        "",
        "Original user request:",
        _clip(task, 4_000),
    ]
    if context.strip():
        parts.extend(["", "Known context:", _clip(context, MAX_CONTEXT_CHARS)])
    if draft.strip():
        parts.extend(["", "Current draft:", _clip(draft, 3_000)])
    parts.extend(["", "Private advisor notes:", "\n\n".join(advice_blocks)])
    return "\n".join(parts)


def render_project_audit_prompt(
    *,
    task: str,
    context: str = "",
    initial_listing: str = "",
) -> str:
    parts = [
        READ_ONLY_AUDIT_PROMPT,
        "",
        "Initial listing:",
        _clip(initial_listing, 4_000) or "(empty)",
        "",
        "User request:",
        _clip(task, 4_000),
    ]
    if context.strip():
        parts.extend(["", "Known context:", _clip(context, MAX_CONTEXT_CHARS)])
    return "\n".join(parts)


def _advisor_timeout(deadline: float) -> float:
    return max(1.0, min(CONSENSUS_ADVISOR_TIMEOUT, deadline - time.monotonic()))


def _audit_path_block_reason(rel: str) -> str:
    normalized = (rel or ".").replace("\\", "/").strip("/")
    if normalized in {"", "."}:
        return ""
    parts = [part for part in normalized.split("/") if part]
    for part in parts:
        lower = part.lower()
        if lower in AUDIT_EXCLUDED_DIRS:
            return "excluded directories are not shared with project audit advisors"
        if lower.startswith("."):
            return "hidden dotfiles are not shared with project audit advisors"
        if lower in AUDIT_SECRET_FILENAMES:
            return "sensitive or lock files are not shared with project audit advisors"
        if any(marker in lower for marker in AUDIT_SECRET_NAME_PARTS):
            return "secret-like paths are not shared with project audit advisors"
        if any(lower.endswith(suffix) for suffix in AUDIT_SECRET_SUFFIXES):
            return "key, certificate, and lock files are not shared with project audit advisors"
        if any(lower.endswith(suffix) for suffix in AUDIT_BINARY_SUFFIXES):
            return "binary files are not shared with project audit advisors"
    return ""


def _audit_raw_symlink_reason(root: Path, rel: str) -> str:
    normalized = (rel or ".").replace("\\", "/").strip("/")
    if normalized in {"", "."}:
        return ""
    current = root
    for part in (part for part in normalized.split("/") if part and part != "."):
        current = current / part
        try:
            if current.is_symlink():
                return "symlinks are not shared with project audit advisors"
        except OSError as exc:
            return str(exc)
    return ""


def _audit_file_allowed(path: Path, root: Path) -> tuple[bool, str]:
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return False, "path escapes project root"
    reason = _audit_path_block_reason(rel)
    if reason:
        return False, reason
    try:
        if path.is_symlink():
            return False, "symlinks are not shared with project audit advisors"
        if not path.is_file():
            return False, "not a file"
        if path.stat().st_size > PROJECT_AUDIT_MAX_FILE_BYTES:
            return False, "file too large for project audit advisors"
    except OSError as exc:
        return False, str(exc)
    return True, ""


def _audit_scannable_file_allowed(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return False
    if _audit_path_block_reason(rel):
        return False
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _audit_visible_entries(root: Path, rel: str) -> ToolOutcome:
    reason = _audit_path_block_reason(rel)
    if reason:
        return ToolOutcome.error(reason)
    reason = _audit_raw_symlink_reason(root, rel)
    if reason:
        return ToolOutcome.error(reason)
    try:
        path = safe_join(root, rel)
    except ValueError as exc:
        return ToolOutcome.error(str(exc))
    if not path.is_dir():
        return ToolOutcome.error(f"not a directory: {rel}")
    lines: list[str] = []
    try:
        entries = tuple(sorted(path.iterdir()))
    except OSError as exc:
        return ToolOutcome.error(str(exc))
    for entry in entries:
        try:
            child_rel = entry.relative_to(root).as_posix()
        except ValueError:
            continue
        if _audit_path_block_reason(child_rel):
            continue
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name in AUDIT_EXCLUDED_DIRS:
                    continue
                lines.append(f"{entry.name}/")
                for sub in sorted(entry.iterdir())[:50]:
                    try:
                        sub_rel = sub.relative_to(root).as_posix()
                    except ValueError:
                        continue
                    if _audit_path_block_reason(sub_rel) or sub.is_symlink():
                        continue
                    tag = "/" if sub.is_dir() else ""
                    lines.append(f"  {sub.name}{tag}")
            elif entry.is_file() and _audit_file_allowed(entry, root)[0]:
                lines.append(entry.name)
        except OSError:
            continue
    return ToolOutcome("\n".join(lines) if lines else "(empty)", True)


def _audit_read_file(root: Path, rel: str, **options) -> ToolOutcome:
    reason = _audit_path_block_reason(rel)
    if reason:
        return ToolOutcome.error(reason)
    reason = _audit_raw_symlink_reason(root, rel)
    if reason:
        return ToolOutcome.error(reason)
    try:
        path = safe_join(root, rel)
    except ValueError as exc:
        return ToolOutcome.error(str(exc))
    allowed, reason = _audit_file_allowed(path, root)
    if not allowed:
        return ToolOutcome.error(reason)
    return read_file(root, rel, **options)


def _audit_dir_allowed(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return False
    return not _audit_path_block_reason(rel)


def _audit_scan_budget() -> BoundedScanBudget:
    return BoundedScanBudget(
        max_files=PROJECT_AUDIT_MAX_SCAN_FILES,
        max_dirs=PROJECT_AUDIT_MAX_SCAN_DIRS,
        max_dir_entries=PROJECT_AUDIT_MAX_DIR_ENTRIES,
    )


def _audit_searchable_files(root: Path, start: Path, budget: BoundedScanBudget):
    resolved_root = root.resolve()
    return iter_bounded_files(
        start,
        excluded_dirs=AUDIT_EXCLUDED_DIRS,
        budget=budget,
        allow_dir=lambda path: _audit_dir_allowed(path, root),
        allow_file=lambda path: _audit_scannable_file_allowed(path, root),
        skip_start_if_excluded=start.resolve() != resolved_root,
    )


def _audit_search_files(
    root: Path,
    rel: str,
    query: str,
    *,
    max_results: int = SEARCH_MAX_RESULTS,
) -> ToolOutcome:
    query = query.strip()
    if not query:
        return ToolOutcome.error("search query required")
    reason = _audit_path_block_reason(rel)
    if reason:
        return ToolOutcome.error(reason)
    reason = _audit_raw_symlink_reason(root, rel)
    if reason:
        return ToolOutcome.error(reason)
    try:
        start = safe_join(root, rel or ".")
    except ValueError as exc:
        return ToolOutcome.error(str(exc))
    if not start.exists():
        return ToolOutcome.error(f"path not found: {rel}")
    needle = query.lower()
    matches: list[str] = []
    result_limited = False
    bytes_read = 0
    byte_limited = False
    oversized_files = 0
    budget = _audit_scan_budget()
    for path in _audit_searchable_files(root, start, budget):
        try:
            size = path.stat().st_size
            if size > SEARCH_MAX_FILE_BYTES:
                oversized_files += 1
                continue
            if bytes_read + size > SEARCH_MAX_SCAN_BYTES:
                byte_limited = True
                break
            bytes_read += size
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if needle not in line.lower():
                continue
            rel_path = path.relative_to(root).as_posix()
            clean = line.strip()
            if len(clean) > 240:
                clean = clean[:237] + "..."
            matches.append(f"{rel_path}:{line_no}: {clean}")
            if len(matches) >= max_results:
                result_limited = True
                break
        if result_limited:
            break
    if not matches:
        matches.append("(no literal matches; regex is not supported)")
    if result_limited:
        matches.append(f"... truncated after {max_results} matches")
    if oversized_files:
        matches.append(
            f"... skipped {oversized_files} oversized file(s); omitted files may "
            "contain more matches"
        )
    if byte_limited:
        matches.append(
            "... project audit search reached its read budget; omitted files may "
            "contain more matches"
        )
    if budget.limited:
        matches.append(budget.stop_message("project audit search scan"))
    output = "\n".join(matches)
    truncated = result_limited or budget.limited or byte_limited or bool(oversized_files)
    if len(output) > READ_MAX_CHARS:
        output = output[:READ_MAX_CHARS].rstrip() + "\n... truncated"
        truncated = True
    return ToolOutcome(output, True, truncated=truncated)


def _audit_find_references(root: Path, rel: str, symbol: str) -> ToolOutcome:
    reason = _audit_path_block_reason(rel)
    if reason:
        return ToolOutcome.error(reason)
    reason = _audit_raw_symlink_reason(root, rel)
    if reason:
        return ToolOutcome.error(reason)
    try:
        start = safe_join(root, rel or ".")
    except ValueError as exc:
        return ToolOutcome.error(str(exc))
    if not start.exists():
        return ToolOutcome.error(f"path not found: {rel}")
    try:
        budget = _audit_scan_budget()
        files = _audit_searchable_files(root, start, budget)
        scan = find_reference_hints(
            root,
            start,
            symbol,
            files=files,
            scan_budget=budget,
        )
    except ValueError as exc:
        return ToolOutcome.error(str(exc))
    return ToolOutcome(scan.output, True, truncated=scan.truncated)


def _execute_read_only_call(project: Path, call: ToolCall) -> ToolOutcome:
    path = str(call.args.get("path") or ".")
    if call.name == "ls":
        return _audit_visible_entries(project, path)
    if call.name == "read":
        read_options = {
            name: call.args[name]
            for name in ("offset", "limit")
            if name in call.args
        }
        return _audit_read_file(project, path, **read_options)
    if call.name == "search":
        return _audit_search_files(project, path, str(call.args.get("query") or ""))
    if call.name == "references":
        return _audit_find_references(project, path, str(call.args.get("symbol") or ""))
    return ToolOutcome.error(
        "project audit advisors may only use read-only list_dir, read_file, grep, and find_references"
    )


def run_project_audit_advisor(
    provider,
    project: str | Path,
    task: str,
    *,
    context: str = "",
    max_turns: int = PROJECT_AUDIT_MAX_TURNS,
) -> str:
    project_path = Path(project).expanduser().resolve()
    initial_listing = _audit_visible_entries(project_path, ".").output
    prompt = render_project_audit_prompt(
        task=task,
        context=context,
        initial_listing=initial_listing,
    )
    deadline = time.monotonic() + PROJECT_AUDIT_ADVISOR_TOTAL_TIMEOUT
    with provider_controls.suppress_assistance():
        reply = provider.send(prompt, timeout=_advisor_timeout(deadline))

    for _turn in range(max(1, max_turns)):
        cancellation.check()
        if time.monotonic() >= deadline:
            return ""
        plan = READ_ONLY_CODEC.parse(reply)
        if plan.protocol_error:
            with provider_controls.suppress_assistance():
                reply = provider.send(
                    f"Protocol error: {plan.protocol_error}\n\n"
                    "Reply with exactly one JSON object using only read-only tools.",
                    timeout=_advisor_timeout(deadline),
                )
            continue
        if plan.calls:
            results: list[ToolResult] = []
            rejected = False
            for call in plan.calls:
                if call.name not in READ_ONLY_TOOL_NAMES:
                    outcome = ToolOutcome.error(
                        "project audit advisors may not edit, write, run, shell, or approve"
                    )
                    rejected = True
                else:
                    try:
                        outcome = _execute_read_only_call(project_path, call)
                    except cancellation.TaskCancelled:
                        raise
                    except Exception as exc:
                        outcome = ToolOutcome.error(str(exc))
                results.append(ToolResult(
                    call=call,
                    output=outcome.output,
                    truncated=outcome.truncated,
                ))
            next_prompt = READ_ONLY_CODEC.format_results(results)
            if rejected:
                next_prompt += (
                    "\n\nYou are in read-only project audit mode. "
                    "Continue with read-only tools or call done(summary)."
                )
            with provider_controls.suppress_assistance():
                reply = provider.send(next_prompt, timeout=_advisor_timeout(deadline))
            continue
        if plan.control is not None and plan.control.kind == "done":
            return _clip(plan.control.body, PROJECT_AUDIT_MAX_REPORT_CHARS)
        with provider_controls.suppress_assistance():
            reply = provider.send(
                READ_ONLY_CODEC.repair_prompt(),
                timeout=_advisor_timeout(deadline),
            )
    return ""


def run_project_audit(
    *,
    project: str | Path,
    selected_provider_id: str,
    task: str,
    provider_ids: Sequence[str],
    provider_labels: Mapping[str, str],
    availability: Callable[[], Mapping[str, bool]],
    connect_existing: Callable[[str], object],
    clear_provider_session: Callable[[str], None] | None = None,
    context: str = "",
    max_advisors: int = MAX_CONSENSUS_ADVISORS,
) -> tuple[ConsensusAdvice, ...]:
    cancellation.check()
    try:
        statuses = dict(availability())
    except Exception:
        statuses = {}
    candidates = advisor_ids(
        selected_provider_id,
        statuses,
        provider_ids,
        max_advisors=max_advisors,
    )
    reports: list[ConsensusAdvice] = []
    for advisor_id in candidates:
        cancellation.check()
        advisor = None
        try:
            advisor = connect_existing(advisor_id)
            if clear_provider_session is not None:
                clear_provider_session(advisor_id)
            advisor.new_chat()
            text = run_project_audit_advisor(
                advisor,
                project,
                task,
                context=context,
            )
            if text.strip():
                reports.append(ConsensusAdvice(
                    advisor_id,
                    provider_labels.get(advisor_id, advisor_id),
                    text,
                ))
        except cancellation.TaskCancelled:
            raise
        except Exception:
            continue
        finally:
            if advisor is not None:
                try:
                    advisor.close()
                except Exception:
                    pass
    return tuple(reports)


def run_consensus(
    *,
    selected_provider,
    selected_provider_id: str,
    task: str,
    provider_ids: Sequence[str],
    provider_labels: Mapping[str, str],
    availability: Callable[[], Mapping[str, bool]],
    connect_existing: Callable[[str], object],
    clear_provider_session: Callable[[str], None] | None = None,
    context: str = "",
    draft: str = "",
    plan: bool = False,
    draft_first: bool = False,
    owner_prompt: str = "",
    max_advisors: int = MAX_CONSENSUS_ADVISORS,
) -> ConsensusResult | None:
    cancellation.check()
    try:
        statuses = dict(availability())
    except Exception:
        statuses = {}
    candidates = advisor_ids(
        selected_provider_id,
        statuses,
        provider_ids,
        max_advisors=max_advisors,
    )
    if not candidates:
        return None

    owner_draft = _clip(draft, 12_000)
    if draft_first:
        prompt = render_owner_draft_prompt(
            task=task,
            context=context,
            plan=plan,
            owner_prompt=owner_prompt,
        )
        with provider_controls.suppress_assistance():
            owner_draft = _clip(
                selected_provider.send(prompt, timeout=CONSENSUS_AGGREGATE_TIMEOUT),
                12_000,
            )
        if not owner_draft:
            raise RuntimeError("consensus draft was empty")

    advices: list[ConsensusAdvice] = []
    for advisor_id in candidates:
        cancellation.check()
        advisor = None
        try:
            advisor = connect_existing(advisor_id)
            if clear_provider_session is not None:
                clear_provider_session(advisor_id)
            advisor.new_chat()
            prompt = render_advisor_prompt(
                task=task,
                context=context,
                draft=owner_draft,
                plan=plan,
            )
            with provider_controls.suppress_assistance():
                text = advisor.send(prompt, timeout=CONSENSUS_ADVISOR_TIMEOUT)
            clipped = _clip(text, MAX_ADVICE_CHARS)
            if clipped:
                advices.append(ConsensusAdvice(
                    advisor_id,
                    provider_labels.get(advisor_id, advisor_id),
                    clipped,
                ))
        except cancellation.TaskCancelled:
            raise
        except Exception:
            continue
        finally:
            if advisor is not None:
                try:
                    advisor.close()
                except Exception:
                    pass

    if not advices:
        if draft_first and owner_draft:
            return ConsensusResult(answer=owner_draft, advisor_count=0, degraded=True)
        return None

    prompt = render_aggregator_prompt(
        task=task,
        advices=advices,
        context=context,
        draft=owner_draft,
        plan=plan,
    )
    try:
        with provider_controls.suppress_assistance():
            answer = selected_provider.send(prompt, timeout=CONSENSUS_AGGREGATE_TIMEOUT)
    except cancellation.TaskCancelled:
        raise
    except Exception:
        if owner_draft:
            return ConsensusResult(
                answer=owner_draft,
                advisor_count=len(advices),
                degraded=True,
            )
        raise
    answer = _clip(answer, 12_000)
    if not answer:
        if owner_draft:
            return ConsensusResult(
                answer=owner_draft,
                advisor_count=len(advices),
                degraded=True,
            )
        raise RuntimeError("consensus answer was empty")
    return ConsensusResult(answer=answer, advisor_count=len(advices))
