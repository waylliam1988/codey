"""Persistent, explainable per-task authorization snapshot.

The policy is built once at the task entry boundary from user-visible intent
(task text, explicit capability flags, the Research button, the task kind) and
then treated as immutable. Model output (``TaskSubmission.model_hint``) can
never grant capabilities. Recovery reuses the stored policy and still passes
through the live path/command/network guards at execution time.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

TASK_POLICY_VERSION = 1

KNOWN_TASK_GRANTS = frozenset({
    "project.read",
    "project.write",
    "project.verify",
    "shell.approval",
    "web.read",
    "knowledge.read",
    "knowledge.write",
    "knowledge.link",
    "control",
})

# 工具授权唯一来源为 ToolSpec（toolchain.tool_spec.spec_for_tool().grant），
# 此处不再维护重复名单，避免漂移。
STRICT_RESEARCH_REQUIRED_CHECKS = (
    "research_sources_opened",
    "research_evidence_saved",
    "research_report_sections",
)


def _normalize_grant(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in KNOWN_TASK_GRANTS else ""


def _normalize_kind(value: object) -> str:
    return str(value or "").strip().lower()


def _has_project(submission: Any) -> bool:
    return bool(str(getattr(submission, "project", "") or "").strip())


def _requested_grants(submission: Any) -> frozenset[str]:
    raw = getattr(submission, "requested_capabilities", ())
    if isinstance(raw, str):
        raw = (raw,)
    try:
        items = tuple(raw or ())
    except TypeError:
        return frozenset()
    grants: set[str] = set()
    for item in items:
        grant = _normalize_grant(item)
        if grant:
            grants.add(grant)
    return frozenset(grants)


def _denied_grants(submission: Any) -> set[str]:
    """Entry-generated explicit denials; control can never be denied."""
    raw = getattr(submission, "denied_capabilities", ())
    if isinstance(raw, str):
        raw = (raw,)
    try:
        items = tuple(raw or ())
    except TypeError:
        return set()
    denied: set[str] = set()
    for item in items:
        grant = _normalize_grant(item)
        if grant and grant != "control":
            denied.add(grant)
    return denied


@dataclass(frozen=True)
class TaskPolicy:
    """Immutable authorization snapshot for one task run."""

    grants: frozenset[str]
    strict_research: bool = False
    required_checks: tuple[str, ...] = ()
    source: str = ""
    version: int = TASK_POLICY_VERSION
    sources_open_required: bool = False
    denied_capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "grants", frozenset(self.grants))
        object.__setattr__(self, "denied_capabilities", frozenset(self.denied_capabilities))
        object.__setattr__(self, "required_checks", tuple(self.required_checks))

    def allows(self, grant: object) -> bool:
        text = _normalize_grant(grant)
        if not text:
            return False
        if text in self.denied_capabilities:
            return False
        return text in self.grants

    def to_payload(self) -> dict[str, object]:
        return {
            "grants": sorted(self.grants or frozenset()),
            "strict_research": bool(self.strict_research),
            "sources_open_required": bool(self.sources_open_required),
            "required_checks": list(self.required_checks or ()),
            "source": str(self.source or ""),
            "version": int(self.version or TASK_POLICY_VERSION),
            "denied_capabilities": sorted(self.denied_capabilities or frozenset()),
        }

    @staticmethod
    def from_payload(payload: object) -> TaskPolicy:
        if not isinstance(payload, dict):
            return TaskPolicy(
                grants=frozenset({"control"}),
                source="recovered:invalid",
                version=TASK_POLICY_VERSION,
            )
        version = payload.get("version")
        if type(version) is not int or version != TASK_POLICY_VERSION:
            # 冷启动无兼容负担：未知版本直接拒绝，由调用方 fail-closed，
            # 绝不按当前格式继续解释。
            raise ValueError(f"unsupported task policy version: {version!r}")
        grants = _restored_grants(payload.get("grants", ()), "grants")
        denied = _restored_grants(payload.get("denied_capabilities", ()), "denied_capabilities")
        grants.add("control")
        raw_checks = payload.get("required_checks", ())
        if not isinstance(raw_checks, (list, tuple)) or any(
            not isinstance(item, str) or not item.strip() for item in raw_checks
        ):
            raise ValueError("required_checks must contain nonempty strings")
        checks = tuple(dict.fromkeys(raw_checks))
        # Preserve every requirement; the completion gate rejects overflow.
        strict = _restored_bool(payload.get("strict_research", False), "strict_research")
        sources_open_required = _restored_bool(payload.get("sources_open_required", False), "sources_open_required")
        source = payload.get("source", "")
        if not isinstance(source, str):
            raise ValueError("source must be a string")
        grants -= denied
        return TaskPolicy(
            grants=frozenset(grants),
            strict_research=strict,
            sources_open_required=sources_open_required,
            required_checks=tuple(checks),
            source=source[:240] if source else "recovered",
            version=TASK_POLICY_VERSION,
            denied_capabilities=frozenset(denied),
        )


def _restored_grants(value: object, field: str) -> set[str]:
    if not isinstance(value, (list, tuple, set, frozenset)) or any(
        not isinstance(item, str) or item not in KNOWN_TASK_GRANTS for item in value
    ):
        raise ValueError(f"{field} must contain known capability names")
    return set(value)


def _restored_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be a boolean")
    return value


def build_task_policy(
    submission: Any,
    *,
    task_kind: object = "project",
    strict_research: object = False,
) -> TaskPolicy:
    """Build the immutable policy for one task from user intent only."""
    kind = _normalize_kind(task_kind)
    # The Research button is user intent; model_hint is never consulted here.
    strict = bool(strict_research is True) or bool(getattr(submission, "strict_research", False) is True)
    sources_open_required = bool(getattr(submission, "sources_open_required", False) is True)
    has_project = _has_project(submission)
    requested = _requested_grants(submission)

    grants: set[str] = {"control"}

    if kind in {"project", "hybrid"}:
        if has_project:
            grants.update({"project.read", "project.verify", "shell.approval"})
            # Strict Research with a project defaults to read/verify; write
            # needs an explicit user request. Plain project/hybrid keep the
            # historical default write grant (the task is to change files).
            if not strict:
                grants.add("project.write")
        if "web.read" in requested:
            grants.add("web.read")
        if kind == "hybrid":
            # Hybrid intentionally mixes web and project tools in one run,
            # including saving research notes before writing code.
            grants.update({"web.read", "knowledge.read", "knowledge.write", "knowledge.link"})
        if strict:
            grants.update({"web.read", "knowledge.read", "knowledge.write", "knowledge.link"})
            if has_project:
                grants.update({"project.read", "project.verify"})
            if "project.write" in requested and has_project:
                grants.add("project.write")
            else:
                # Strict Research without an explicit project.write request
                # never keeps write or the shell request surface, for any
                # task kind (project/hybrid/research).
                grants.discard("project.write")
                grants.discard("shell.approval")
    elif kind == "research":
        grants.update({"web.read", "knowledge.read", "knowledge.write", "knowledge.link"})
        if has_project:
            grants.update({"project.read", "project.verify"})
        if "project.write" in requested and has_project:
            grants.add("project.write")
    elif kind in {"planning", "planning_readonly", "readonly"}:
        if has_project:
            grants.add("project.read")
        if "web.read" in requested:
            grants.add("web.read")
        # Read-only planning never grants write/shell, even when requested.
        grants.discard("project.write")
        grants.discard("shell.approval")
    else:  # chat, review, unknown kinds: minimal surface.
        if "web.read" in requested:
            grants.add("web.read")

    if strict and not ("project.write" in requested and has_project):
        # Single strict-research gate for every kind: without an explicit
        # user project.write grant, neither project.write nor the shell
        # request surface may survive, no matter which branch added them.
        grants.discard("project.write")
        grants.discard("shell.approval")
    if not has_project:
        grants.discard("project.read")
        grants.discard("project.write")
        grants.discard("project.verify")
        grants.discard("shell.approval")

    # Unknown requested capabilities are ignored (denied); they never widen grants.
    grants.intersection_update(KNOWN_TASK_GRANTS | {"control"})
    if "control" not in grants:
        grants.add("control")

    # 入口显式否决最后统一扣除：默认授权、requested、strict 分支都不能加回。
    denied = _denied_grants(submission)
    grants -= denied

    required_checks = STRICT_RESEARCH_REQUIRED_CHECKS if strict else (("research_sources_opened",) if sources_open_required else ())
    requested_text = ",".join(sorted(requested)) if requested else "-"
    denied_text = ",".join(sorted(denied)) if denied else "-"
    source = f"kind:{kind or 'project'};project:{'yes' if has_project else 'no'};requested:{requested_text};strict:{'yes' if strict else 'no'};denied:{denied_text}"
    return TaskPolicy(
        grants=frozenset(grants),
        strict_research=strict,
        sources_open_required=sources_open_required,
        required_checks=tuple(required_checks),
        source=source,
        version=TASK_POLICY_VERSION,
        denied_capabilities=frozenset(denied),
    )


def _grant_for_tool_name(name: str) -> str:
    try:
        from codey.toolchain.tool_spec import spec_for_tool as _spec_for

        spec = _spec_for(name)
        return str(getattr(spec, "grant", "") or "") if spec is not None else ""
    except Exception:
        return ""


def _alias_allowed(canonical: str, allowed: set[str]) -> bool:
    if canonical == "open_url":
        return bool({"open_result", "reopen_source", "open_hit"} & allowed)
    return False


def policy_for_dispatch(request: object, kind: object, *, strict_research: object = False) -> TaskPolicy | None:
    """Build the immutable TaskPolicy for one dispatch kind (user intent only).

    Moved from operations.task_loop so policy lives with policy; task_loop
    re-exports it for backward compatibility.
    """
    try:
        return build_task_policy(request, task_kind=kind, strict_research=strict_research)
    except Exception:
        return None


def apply_auto_plan(policy: object, plan_text: object) -> object:
    """Narrow a policy by an auto PLAN; the plan can never widen grants."""
    if policy is None or not callable(getattr(policy, "allows", None)):
        return policy
    text = str(plan_text or "")
    lowered = text.lower()
    if "action:" not in lowered and "plan:" not in lowered:
        return policy
    grants = set(getattr(policy, "grants", frozenset()) or frozenset())
    if "action: project" not in lowered and "action:project" not in lowered:
        grants.discard("project.write")
        grants.discard("shell.approval")
    if "action: research" not in lowered and "action:research" not in lowered:
        grants.discard("web.read")
        grants.discard("knowledge.read")
        grants.discard("knowledge.write")
        grants.discard("knowledge.link")
    grants.add("control")
    return replace(policy, grants=frozenset(grants), source=policy.source + ";auto_narrowed")



__all__ = [
    "KNOWN_TASK_GRANTS",
    "STRICT_RESEARCH_REQUIRED_CHECKS",
    "TASK_POLICY_VERSION",
    "TaskPolicy",
    "apply_auto_plan",
    "build_task_policy",
    "policy_for_dispatch",
]
