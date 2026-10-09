"""Checked-in reference snapshots match their portable source digests."""
import hashlib
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize('name', ['opencode', 'pi'])
def test_checked_in_reference_sources_match_the_recorded_lf_digest(name):
    root = Path(__file__).resolve().parent / 'fixtures' / f'{name}_compaction_reference'
    manifest = json.loads((root / 'provenance.json').read_text(encoding='utf-8'))
    assert manifest.get('normalization') == 'LF'
    for relative, expected in manifest['files'].items():
        actual = hashlib.sha256((root / relative).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
        assert actual == expected, relative
