"""Compatibility re-export: ScanReport now lives in codey.utils.scan_report.

The canonical implementation moved down to utils so bounded-scan producers
(toolchain, utils) no longer import the reviews package. Import from
codey.utils.scan_report in new code.
"""

from __future__ import annotations

from codey.utils.scan_report import (
    MAX_SCAN_REPORT_EXAMPLES,
    ScanReport,
    render_scan_coverage,
)

__all__ = ["MAX_SCAN_REPORT_EXAMPLES", "ScanReport", "render_scan_coverage"]
