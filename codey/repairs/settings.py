"""Local opt-in for automatic adapter repair and candidate activation."""

from pathlib import Path

SELF_REPAIR_SWITCH = "enable-self-repair"


def self_repair_source(state_home: Path) -> Path | None:
    try:
        value = (state_home / SELF_REPAIR_SWITCH).read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        return None
    source = Path(value)
    if not source.is_absolute() or not (source / "pyproject.toml").is_file() or not (source / "tests").is_dir():
        return None
    return source
