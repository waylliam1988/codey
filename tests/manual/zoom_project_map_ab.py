"""Manual A/B for a layered Project Map / zoom-map navigation strategy.

This probe builds a temporary deep synthetic repository, then asks a live web
provider to choose the first files it would inspect under two read-only arms:

* current: the legacy task-aware Project Map shape, including Symbol overview
  and excluding Focused subtree
* zoom: the production task-aware Project Map, including Focused subtree

The fixture intentionally includes deep target files that are not named in most
tasks, plus enough earlier filler files to push those targets beyond the legacy
bounded Symbol overview scan. The report separates unnamed deep cases from named
controls so the pre-registered success/kill condition can be evaluated without
ceiling effects.
"""

# ruff: noqa: E402 - direct script execution must add the repository root first.

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codey.providers import controls as provider_controls
from codey.workspace.map import (
    build_project_map,
    build_symbol_overview,
)
from codey.providers.registry import DEFAULT_PROVIDER_ID, connect_provider, provider_ids
from codey.toolchain.runtime import list_directory
from tests.manual.project_task_context import render_production_project_map

DEFAULT_OUTPUT = Path(tempfile.gettempdir()) / "codey-zoom-project-map-ab.json"
ARMS = ("current", "zoom")
MAX_RAW_REPLY_CHARS = 3_000

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except (AttributeError, OSError):
    pass


@dataclass(frozen=True)
class ProbeCase:
    name: str
    task: str
    expected_paths: tuple[str, ...]
    expected_tests: tuple[str, ...] = ()
    target_named_in_task: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)

class CountingProvider:
    def __init__(self, provider) -> None:
        self.provider = provider
        self.name = provider.name
        self.sends = 0
        self.sent_chars = 0
        self.reply_chars = 0
        self.seconds = 0.0

    def __getattr__(self, name: str):
        return getattr(self.provider, name)

    def send(self, text: str, timeout: float | None = None) -> str:
        self.sends += 1
        self.sent_chars += len(text or "")
        started = time.monotonic()
        reply = self.provider.send(text, timeout=timeout)
        self.seconds += time.monotonic() - started
        self.reply_chars += len(reply or "")
        return reply


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")


def build_deep_fixture(root: Path) -> dict[str, ProbeCase]:
    """Create a deterministic deep repo where current shallow maps hit limits."""
    root.mkdir(parents=True, exist_ok=True)
    _write(
        root / "README.md",
        """
        # Deep Commerce Platform

        Synthetic monorepo for Project Map navigation probes.
        """,
    )
    _write(
        root / "pyproject.toml",
        """
        [tool.pytest.ini_options]
        testpaths = ["apps", "services", "packages"]
        """,
    )

    # Alphabetically early filler under apps/admin is deliberate: the current
    # bounded Symbol overview sees only the first 120 source files, so the real
    # targets below apps/commerce, services, and packages stay out of that view.
    for index in range(150):
        _write(
            root / "apps" / "admin" / "src" / "generated" / f"admin_report_{index:03d}.py",
            f"""
            class AdminReport{index:03d}:
                def render_overview(self, user, flags):
                    return "admin-report-{index:03d}"

            def build_admin_report_{index:03d}(config):
                return AdminReport{index:03d}()
            """,
        )

    _write(
        root
        / "apps"
        / "commerce"
        / "src"
        / "domain"
        / "billing"
        / "policies"
        / "proration_policy.py",
        """
        class SubscriptionProrationPolicy:
            def calculate_unused_credit(self, previous_plan, new_plan, period):
                return period.remaining_ratio * previous_plan.monthly_price

            def build_invoice_adjustment(self, credit, upgrade_delta):
                return {"credit": credit, "delta": upgrade_delta}

        def preview_subscription_upgrade(account, requested_plan, clock):
            policy = SubscriptionProrationPolicy()
            return policy.build_invoice_adjustment(
                policy.calculate_unused_credit(account.plan, requested_plan, clock.period),
                requested_plan.monthly_price - account.plan.monthly_price,
            )
        """,
    )
    _write(
        root
        / "apps"
        / "commerce"
        / "src"
        / "domain"
        / "billing"
        / "invoices"
        / "adjustment_builder.py",
        """
        def create_invoice_adjustment(subscription_id, credit, delta):
            return {
                "subscription_id": subscription_id,
                "unused_credit": credit,
                "upgrade_delta": delta,
            }

        def attach_adjustment_to_draft_invoice(invoice, adjustment):
            invoice.adjustments.append(adjustment)
            return invoice
        """,
    )
    _write(
        root
        / "apps"
        / "commerce"
        / "tests"
        / "billing"
        / "test_proration_policy.py",
        """
        def test_subscription_upgrade_unused_credit_creates_adjustment():
            assert True
        """,
    )

    _write(
        root
        / "services"
        / "messaging"
        / "src"
        / "pipelines"
        / "digest"
        / "quiet_hours.py",
        """
        class DigestQuietHours:
            def suppress_during_user_quiet_window(self, user, send_time):
                return user.quiet_window.contains(send_time)

            def locale_send_window(self, user):
                return user.locale.default_digest_window

        def should_suppress_digest_delivery(user, send_time):
            return DigestQuietHours().suppress_during_user_quiet_window(user, send_time)
        """,
    )
    _write(
        root
        / "services"
        / "messaging"
        / "src"
        / "pipelines"
        / "digest"
        / "digest_scheduler.py",
        """
        def schedule_daily_digest(user, clock, quiet_hours):
            if quiet_hours.suppress_during_user_quiet_window(user, clock.now()):
                return "suppressed"
            return "scheduled"
        """,
    )
    _write(
        root
        / "services"
        / "messaging"
        / "tests"
        / "digest"
        / "test_quiet_hours.py",
        """
        def test_daily_digest_suppressed_during_quiet_window():
            assert True
        """,
    )

    _write(
        root
        / "packages"
        / "gateway"
        / "src"
        / "http"
        / "middleware"
        / "rate_limit_window.py",
        """
        class TenantRollingWindowLimiter:
            def compute_burst_allowance(self, tenant_id, request_time):
                return {"tenant_id": tenant_id, "remaining": 42}

            def should_throttle_request(self, tenant_id, request_time):
                return self.compute_burst_allowance(tenant_id, request_time)["remaining"] <= 0
        """,
    )
    _write(
        root
        / "packages"
        / "gateway"
        / "src"
        / "http"
        / "middleware"
        / "retry_after.py",
        """
        def build_retry_metadata(tenant_id, rolling_window, now):
            return {"tenant_id": tenant_id, "retry_after": rolling_window.reset_after(now)}
        """,
    )
    _write(
        root
        / "packages"
        / "gateway"
        / "tests"
        / "http"
        / "test_rate_limit_window.py",
        """
        def test_gateway_burst_limiter_returns_retry_metadata():
            assert True
        """,
    )

    _write(
        root
        / "services"
        / "fulfillment"
        / "src"
        / "integrations"
        / "carriers"
        / "event_dedupe.py",
        """
        class CarrierEventDeduplicator:
            def deduplicate_callback(self, carrier_event, idempotency_store):
                return idempotency_store.claim_once(carrier_event.external_id)

        def should_advance_from_carrier_callback(carrier_event, store):
            return CarrierEventDeduplicator().deduplicate_callback(carrier_event, store)
        """,
    )
    _write(
        root
        / "services"
        / "fulfillment"
        / "src"
        / "workflows"
        / "advance_order_state.py",
        """
        def advance_order_fulfillment_state(order, carrier_event):
            order.apply_carrier_event(carrier_event)
            return order
        """,
    )
    _write(
        root
        / "services"
        / "fulfillment"
        / "tests"
        / "carriers"
        / "test_event_dedupe.py",
        """
        def test_carrier_callback_is_idempotent_before_advancing_order():
            assert True
        """,
    )

    # Shallow decoys make path guessing less reliable for the current arm.
    _write(
        root / "scripts" / "billing_report.py",
        """
        def export_billing_report(month):
            return f"billing report {month}"
        """,
    )
    _write(
        root / "tools" / "digest_audit.py",
        """
        def audit_digest_templates():
            return []
        """,
    )

    return {
        "billing-proration": ProbeCase(
            name="billing-proration",
            task=(
                "Find where subscription upgrades calculate unused credit and "
                "create invoice adjustments in the billing flow. Include likely "
                "focused tests."
            ),
            expected_paths=(
                "apps/commerce/src/domain/billing/policies/proration_policy.py",
                "apps/commerce/src/domain/billing/invoices/adjustment_builder.py",
            ),
            expected_tests=("apps/commerce/tests/billing/test_proration_policy.py",),
            tags=("deep", "unnamed-target", "billing"),
        ),
        "digest-quiet-window": ProbeCase(
            name="digest-quiet-window",
            task=(
                "Find where daily digest delivery is suppressed during user "
                "quiet windows and locale send windows. Include likely focused "
                "tests."
            ),
            expected_paths=(
                "services/messaging/src/pipelines/digest/quiet_hours.py",
                "services/messaging/src/pipelines/digest/digest_scheduler.py",
            ),
            expected_tests=("services/messaging/tests/digest/test_quiet_hours.py",),
            tags=("deep", "unnamed-target", "messaging"),
        ),
        "gateway-burst-window": ProbeCase(
            name="gateway-burst-window",
            task=(
                "Find where API gateway burst limiting computes per-tenant "
                "rolling windows and retry metadata. Include likely focused "
                "tests."
            ),
            expected_paths=(
                "packages/gateway/src/http/middleware/rate_limit_window.py",
                "packages/gateway/src/http/middleware/retry_after.py",
            ),
            expected_tests=("packages/gateway/tests/http/test_rate_limit_window.py",),
            tags=("deep", "unnamed-target", "gateway"),
        ),
        "fulfillment-idempotency": ProbeCase(
            name="fulfillment-idempotency",
            task=(
                "Find where warehouse shipment callbacks deduplicate carrier "
                "events before order fulfillment state is advanced. Include "
                "likely focused tests."
            ),
            expected_paths=(
                "services/fulfillment/src/integrations/carriers/event_dedupe.py",
                "services/fulfillment/src/workflows/advance_order_state.py",
            ),
            expected_tests=("services/fulfillment/tests/carriers/test_event_dedupe.py",),
            tags=("deep", "unnamed-target", "fulfillment"),
        ),
        "named-proration-control": ProbeCase(
            name="named-proration-control",
            task=(
                "Find proration_policy.py and the focused tests for subscription "
                "upgrade unused-credit invoice adjustments."
            ),
            expected_paths=("apps/commerce/src/domain/billing/policies/proration_policy.py",),
            expected_tests=("apps/commerce/tests/billing/test_proration_policy.py",),
            target_named_in_task=True,
            tags=("deep", "named-control", "billing"),
        ),
        "named-quiet-hours-control": ProbeCase(
            name="named-quiet-hours-control",
            task=(
                "Find quiet_hours.py and the focused tests for daily digest quiet "
                "window suppression."
            ),
            expected_paths=("services/messaging/src/pipelines/digest/quiet_hours.py",),
            expected_tests=("services/messaging/tests/digest/test_quiet_hours.py",),
            target_named_in_task=True,
            tags=("deep", "named-control", "messaging"),
        ),
    }


def _clip(value: object, limit: int = MAX_RAW_REPLY_CHARS) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[truncated]"


def _extract_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text or ""):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _normalize_path(value: object) -> str:
    text = str(value or "").replace("\\", "/").strip().strip("\"'")
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def _coerce_path_list(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        raw_items: list[object] = [value]
    elif isinstance(value, list):
        raw_items = value
    else:
        return ()
    paths: list[str] = []
    for item in raw_items:
        path = _normalize_path(item)
        if path and path not in paths:
            paths.append(path)
    return tuple(paths)


def _paths_from_reply(text: str) -> tuple[str, ...]:
    obj = _extract_json_object(text)
    return _coerce_path_list(obj.get("paths"))


def _test_paths_from_reply(text: str) -> tuple[str, ...]:
    obj = _extract_json_object(text)
    return _coerce_path_list(obj.get("test_paths") or obj.get("tests"))


def _path_matches(actual: str, expected: str) -> bool:
    actual = _normalize_path(actual)
    expected = _normalize_path(expected)
    return (
        actual == expected
        or actual.endswith("/" + expected)
        or expected.endswith("/" + actual)
    )


def _score_paths(paths: tuple[str, ...], expected: tuple[str, ...]) -> dict[str, Any]:
    hits: list[str] = []
    first_expected_rank = None
    for expected_path in expected:
        if any(_path_matches(path, expected_path) for path in paths):
            hits.append(expected_path)
    for index, path in enumerate(paths, start=1):
        if any(_path_matches(path, expected_path) for expected_path in expected):
            first_expected_rank = index
            break
    return {
        "hits": hits,
        "hit_count": len(hits),
        "first_expected_rank": first_expected_rank,
        "top1_hit": first_expected_rank == 1,
    }


def _score_result(
    case: ProbeCase,
    paths: tuple[str, ...],
    test_paths: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "paths": _score_paths(paths, case.expected_paths),
        "tests": _score_paths(test_paths, case.expected_tests),
    }


def render_legacy_project_map(root: Path, task: str) -> str:
    project_map = build_project_map(root, task=task)
    return replace(
        project_map,
        focused_subtree="",
        symbol_overview=build_symbol_overview(root, task),
    ).render()


def render_zoom_project_map(root: Path, task: str) -> str:
    return render_production_project_map(root, task=task)


def _selection_prompt(case: ProbeCase, root: Path, *, arm: str) -> str:
    if arm == "current":
        map_text = render_legacy_project_map(root, case.task)
    elif arm == "zoom":
        map_text = render_zoom_project_map(root, case.task)
    else:
        raise ValueError(f"unknown arm: {arm}")

    listing = list_directory(root, ".").model_text
    return "\n".join(
        [
            "You are helping evaluate a local coding agent's project navigation.",
            "Do not solve the code change. Only choose files to inspect first.",
            "Prefer exact relative file paths from the map. Do not invent paths.",
            "Return exactly one JSON object, no markdown:",
            (
                '{"paths":["relative/implementation.py"],'
                '"test_paths":["relative/test_file.py"],'
                '"reason":"short reason"}'
            ),
            "",
            f"Project root: {root}",
            f"Map arm: {arm}",
            "Initial listing:",
            listing,
            "",
            map_text,
            "",
            "Task:",
            case.task,
        ]
    )


def _fresh_chat(provider: CountingProvider) -> None:
    new_chat = getattr(provider, "new_chat", None)
    if callable(new_chat):
        new_chat()


def run_arm(
    provider: CountingProvider,
    case: ProbeCase,
    root: Path,
    arm: str,
    *,
    timeout: float,
    fresh_chat: bool,
) -> dict[str, Any]:
    if fresh_chat:
        _fresh_chat(provider)
    sent_before = provider.sent_chars
    reply_before = provider.reply_chars
    sends_before = provider.sends
    seconds_before = provider.seconds
    started = time.monotonic()

    prompt = _selection_prompt(case, root, arm=arm)
    reply = provider.send(prompt, timeout=timeout)
    paths = _paths_from_reply(reply)
    test_paths = _test_paths_from_reply(reply)
    score = _score_result(case, paths, test_paths)
    return {
        "case": case.name,
        "arm": arm,
        "tags": list(case.tags),
        "target_named_in_task": case.target_named_in_task,
        "task": case.task,
        "expected_paths": list(case.expected_paths),
        "expected_tests": list(case.expected_tests),
        "paths": list(paths),
        "test_paths": list(test_paths),
        "score": score,
        "ok": score["paths"]["hit_count"] > 0 or score["tests"]["hit_count"] > 0,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "provider_seconds": round(provider.seconds - seconds_before, 3),
        "sends": provider.sends - sends_before,
        "sent_chars": provider.sent_chars - sent_before,
        "reply_chars": provider.reply_chars - reply_before,
        "prompt_chars": len(prompt),
        "raw_reply": _clip(reply),
    }


def _summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [row for row in rows if "error" not in row]
    summary: dict[str, Any] = {
        "completed_rows": len(completed),
        "errors": len(rows) - len(completed),
        "by_arm": {},
        "deltas_vs_current": {},
    }
    for arm in ARMS:
        arm_rows = [row for row in completed if row.get("arm") == arm]
        summary["by_arm"][arm] = {
            "rows": len(arm_rows),
            "ok": sum(1 for row in arm_rows if row.get("ok")),
            "path_hits": sum(row["score"]["paths"]["hit_count"] for row in arm_rows),
            "test_hits": sum(row["score"]["tests"]["hit_count"] for row in arm_rows),
            "top1_path_hits": sum(
                1 for row in arm_rows if row["score"]["paths"]["top1_hit"]
            ),
            "sent_chars": sum(int(row.get("sent_chars") or 0) for row in arm_rows),
            "prompt_chars": sum(int(row.get("prompt_chars") or 0) for row in arm_rows),
            "provider_seconds": round(
                sum(float(row.get("provider_seconds") or 0.0) for row in arm_rows),
                3,
            ),
        }
    current = summary["by_arm"]["current"]
    zoom = summary["by_arm"]["zoom"]
    delta: dict[str, Any] = {}
    for key in ("ok", "path_hits", "test_hits", "top1_path_hits"):
        delta[key] = zoom[key] - current[key]
    delta["sent_chars"] = zoom["sent_chars"] - current["sent_chars"]
    delta["prompt_chars"] = zoom["prompt_chars"] - current["prompt_chars"]
    delta["provider_seconds"] = round(
        zoom["provider_seconds"] - current["provider_seconds"],
        3,
    )
    summary["deltas_vs_current"]["zoom"] = delta
    return summary


def _summary_with_subsets(rows: list[dict[str, Any]]) -> dict[str, Any]:
    unnamed = [
        row
        for row in rows
        if "error" not in row
        and not row.get("target_named_in_task")
        and "deep" in row.get("tags", [])
    ]
    named = [
        row
        for row in rows
        if "error" not in row
        and row.get("target_named_in_task")
        and "deep" in row.get("tags", [])
    ]
    return {
        "all": _summarize_rows(rows),
        "unnamed_deep": _summarize_rows(unnamed),
        "named_controls": _summarize_rows(named),
    }


def _write_report(
    output: Path,
    *,
    providers: tuple[str, ...],
    fixture_root: Path,
    selected_cases: tuple[ProbeCase, ...],
    provider_reports: list[dict[str, Any]],
    partial: bool,
) -> dict[str, Any]:
    rows = [row for report in provider_reports for row in report.get("rows", [])]
    report = {
        "probe": "zoom_project_map_ab",
        "providers": list(providers),
        "completed_providers": [str(report.get("provider") or "") for report in provider_reports],
        "partial": partial,
        "fixture_root": fixture_root.as_posix(),
        "case_count": len(selected_cases),
        "cases": [
            {
                "name": case.name,
                "target_named_in_task": case.target_named_in_task,
                "tags": list(case.tags),
                "expected_paths": list(case.expected_paths),
                "expected_tests": list(case.expected_tests),
            }
            for case in selected_cases
        ],
        "provider_reports": provider_reports,
        "summary": _summary_with_subsets(rows),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text + "\n", encoding="utf-8")
    return report


def _arm_order(order: str) -> tuple[str, ...]:
    if order == "zoom-first":
        return ("zoom", "current")
    if order == "current-first":
        return ("current", "zoom")
    raise ValueError(f"unknown order: {order}")


def run_provider(
    provider_id: str,
    selected_cases: tuple[ProbeCase, ...],
    root: Path,
    *,
    port: int,
    timeout: float,
    order: str,
    fresh_chat: bool,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    raw = None
    provider_controls.begin_task_context(f"zoom-project-map-ab:{provider_id}")
    try:
        raw = connect_provider(provider_id, port=port)
        provider = CountingProvider(raw)
        for case in selected_cases:
            for arm in _arm_order(order):
                try:
                    row = run_arm(
                        provider,
                        case,
                        root,
                        arm,
                        timeout=timeout,
                        fresh_chat=fresh_chat,
                    )
                    row["provider"] = provider_id
                except Exception as exc:
                    row = {
                        "provider": provider_id,
                        "case": case.name,
                        "arm": arm,
                        "error": str(exc),
                    }
                rows.append(row)
                status = "ERR" if "error" in row else (
                    f"paths={row['score']['paths']['hit_count']} "
                    f"tests={row['score']['tests']['hit_count']} "
                    f"top1={int(row['score']['paths']['top1_hit'])} "
                    f"chars={row['sent_chars']}"
                )
                print(f"[{provider_id}] {case.name} {arm}: {status}", flush=True)
    finally:
        try:
            if raw is not None:
                raw.close()
        finally:
            provider_controls.end_task_context()
    return {
        "provider": provider_id,
        "rows": rows,
        "summary": _summary_with_subsets(rows),
    }


def run_self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="codey-zoom-map-selftest-") as td:
        root = Path(td)
        available = build_deep_fixture(root)
        case = available["billing-proration"]
        current_map = render_legacy_project_map(root, case.task)
        zoom_map = render_zoom_project_map(root, case.task)
        assert case.expected_paths[0] not in current_map, current_map
        assert case.expected_paths[0] in zoom_map, zoom_map
        assert "apps/commerce/" in zoom_map, zoom_map
        prompt = _selection_prompt(case, root, arm="zoom")
        assert "Focused subtree" in prompt
        assert "proration_policy.py" in prompt
        parsed = _paths_from_reply(
            '{"paths":["./proration_policy.py"],'
            '"test_paths":["apps/commerce/tests/billing/test_proration_policy.py"]}'
        )
        assert parsed == ("proration_policy.py",)
        score = _score_paths(("proration_policy.py",), case.expected_paths)
        assert score["hit_count"] == 1, score
        rows = [
            {
                "arm": "current",
                "target_named_in_task": False,
                "tags": ["deep"],
                "ok": False,
                "score": {
                    "paths": {"hit_count": 0, "top1_hit": False},
                    "tests": {"hit_count": 0},
                },
                "sent_chars": 100,
                "prompt_chars": 100,
                "provider_seconds": 1.0,
            },
            {
                "arm": "zoom",
                "target_named_in_task": False,
                "tags": ["deep"],
                "ok": True,
                "score": {
                    "paths": {"hit_count": 1, "top1_hit": True},
                    "tests": {"hit_count": 1},
                },
                "sent_chars": 90,
                "prompt_chars": 90,
                "provider_seconds": 1.5,
            },
        ]
        summary = _summary_with_subsets(rows)
        delta = summary["unnamed_deep"]["deltas_vs_current"]["zoom"]
        assert delta["top1_path_hits"] == 1, delta
        assert delta["sent_chars"] == -10, delta
    print("self-test passed")


def _prepare_fixture(args: argparse.Namespace):
    if args.fixture_root is not None:
        root = args.fixture_root.expanduser().resolve()
        cases = build_deep_fixture(root)
        return root, cases, None
    manager = tempfile.TemporaryDirectory(prefix="codey-zoom-map-fixture-")
    root = Path(manager.name)
    cases = build_deep_fixture(root)
    return root, cases, manager


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare legacy Project Map navigation with Focused subtree."
    )
    parser.add_argument("--provider", choices=(*provider_ids(), "all"), default=DEFAULT_PROVIDER_ID)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--case", action="append")
    parser.add_argument("--max-cases", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--order",
        choices=("current-first", "zoom-first"),
        default="current-first",
    )
    parser.add_argument(
        "--reuse-chat",
        action="store_true",
        help="Do not open a fresh provider chat before each arm.",
    )
    parser.add_argument("--fixture-root", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--print-maps", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build fixture and optional maps, then exit before opening providers.",
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        run_self_test()
        return 0

    root, available, manager = _prepare_fixture(args)
    try:
        if args.case:
            missing = [name for name in args.case if name not in available]
            if missing:
                parser.error(f"unknown case(s): {', '.join(missing)}")
            selected = tuple(available[name] for name in args.case)
        else:
            selected = tuple(available.values())
        if args.max_cases > 0:
            selected = selected[: args.max_cases]
        if not selected:
            parser.error("no probe cases selected")

        if args.print_maps:
            for case in selected:
                current = render_legacy_project_map(root, case.task)
                zoom = render_zoom_project_map(root, case.task)
                print(
                    f"[{case.name}] current_chars={len(current)} "
                    f"zoom_chars={len(zoom)}"
                )
                print(zoom)
        if args.dry_run:
            return 0

        selected_providers = provider_ids() if args.provider == "all" else (args.provider,)
        reports: list[dict[str, Any]] = []
        report: dict[str, Any] | None = None
        for provider_id in selected_providers:
            reports.append(
                run_provider(
                    provider_id,
                    selected,
                    root,
                    port=args.port,
                    timeout=args.timeout,
                    order=args.order,
                    fresh_chat=not args.reuse_chat,
                )
            )
            report = _write_report(
                args.output,
                providers=selected_providers,
                fixture_root=root,
                selected_cases=selected,
                provider_reports=reports,
                partial=len(reports) < len(selected_providers),
            )
        if report is None:
            report = _write_report(
                args.output,
                providers=selected_providers,
                fixture_root=root,
                selected_cases=selected,
                provider_reports=reports,
                partial=False,
            )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["summary"]["all"]["completed_rows"] else 1
    finally:
        if manager is not None:
            manager.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())