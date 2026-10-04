"""Reuse requires a successful source; invalid IDs never alias storage paths."""
import pytest

from codey.reviews.persistence import ReviewArtifactStore
from codey.reviews.reuse import validate_source_run_id


@pytest.mark.parametrize("value", [True, 42, [], "a:b", "a b", "a\\b", "a/../b"])
def test_invalid_source_id_is_rejected(value):
    with pytest.raises(ValueError):
        validate_source_run_id(value)


def test_storage_does_not_sanitize_colliding_attempt_ids(tmp_path):
    store = ReviewArtifactStore(tmp_path)
    with pytest.raises(ValueError):
        store.path_for("s", "r", "a:b")
