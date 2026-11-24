from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

from codey.completion.verification_policy import discover_verification_candidates


def test_discovery_keeps_candidates_from_readable_sibling(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    readable = root / "readable"
    readable.mkdir()
    (readable / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    inaccessible = root / "inaccessible"
    inaccessible.mkdir()
    original_iterdir = Path.iterdir

    def delayed_iterdir(directory: Path) -> Iterator[Path]:
        if directory == inaccessible:
            raise PermissionError("cannot enumerate directory")
        if directory == root:
            yield readable
            yield inaccessible
        else:
            yield from original_iterdir(directory)

    with (
        patch.object(Path, "iterdir", new=delayed_iterdir),
        patch("codey.completion.verification_policy.shutil.which", return_value="python"),
    ):
        candidates = discover_verification_candidates(root)

    assert tuple((candidate.command, candidate.cwd) for candidate in candidates) == (
        ("python -m pytest", "readable"),
    )
