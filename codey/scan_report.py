"""Small scan omission facts for bounded local tools."""

from __future__ import annotations

from dataclasses import dataclass, field


MAX_SCAN_REPORT_EXAMPLES = 3


@dataclass
class ScanReport:
    label: str
    size_limit_bytes: int | None = None
    oversized: int = 0
    unreadable: int = 0
    decode_failed: int = 0
    oversized_examples: list[str] = field(default_factory=list)
    unreadable_examples: list[str] = field(default_factory=list)
    decode_failed_examples: list[str] = field(default_factory=list)

    @property
    def incomplete(self) -> bool:
        return bool(self.oversized or self.unreadable or self.decode_failed)

    def add_oversized(self, path: str) -> None:
        self.oversized += 1
        _append_example(self.oversized_examples, path)

    def add_unreadable(self, path: str) -> None:
        self.unreadable += 1
        _append_example(self.unreadable_examples, path)

    def add_decode_failed(self, path: str) -> None:
        self.decode_failed += 1
        _append_example(self.decode_failed_examples, path)


def render_scan_coverage(report: ScanReport) -> str:
    if not report.incomplete:
        return ""
    lines = ["Scan coverage:"]
    if report.oversized:
        plural = "file" if report.oversized == 1 else "files"
        limit = (
            f" over {_byte_limit_label(report.size_limit_bytes)}"
            if report.size_limit_bytes is not None
            else ""
        )
        lines.append(
            f"- {report.label} skipped {report.oversized} oversized {plural}{limit}; "
            "omitted files may contain more references"
        )
        _append_examples(lines, "oversized path examples", report.oversized_examples)
    if report.unreadable:
        plural = "file" if report.unreadable == 1 else "files"
        lines.append(
            f"- {report.label} could not read metadata or contents for "
            f"{report.unreadable} {plural}; omitted files may contain more references"
        )
        _append_examples(lines, "unreadable path examples", report.unreadable_examples)
    if report.decode_failed:
        plural = "file" if report.decode_failed == 1 else "files"
        lines.append(
            f"- {report.label} skipped {report.decode_failed} non-UTF-8 {plural}; "
            "omitted files may contain more references"
        )
        _append_examples(lines, "decode failure path examples", report.decode_failed_examples)
    return "\n".join(lines)


def _append_example(values: list[str], path: str) -> None:
    if path and len(values) < MAX_SCAN_REPORT_EXAMPLES:
        values.append(path)


def _append_examples(lines: list[str], label: str, examples: list[str]) -> None:
    if examples:
        lines.append(f"- {label}: {', '.join(examples)}")


def _byte_limit_label(value: int) -> str:
    if value >= 1024 * 1024:
        return f"{value // (1024 * 1024)} MiB"
    if value >= 1024:
        return f"{value // 1024} KiB"
    return f"{value} bytes"
