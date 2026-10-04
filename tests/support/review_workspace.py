"""Real workspace identities for Coordinator unit-test callbacks."""
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from codey.reviews.identity import build_identity, capture_snapshot
from codey.reviews.input import prepare_review_input


@contextmanager
def review_workspace(run_review):
    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "app.py").write_text("x", encoding="utf-8")
        prepared = prepare_review_input(project=directory, task="review", writer_summary="", changes={
            "ok": True, "files": [{"path": "app.py", "status": "M"}], "diff": "", "changed_count": 1,
        })
        identity = build_identity(prepared, reviewer_id="reviewer", policy="web_if_available",
                                  project=directory, snapshot=capture_snapshot(root, ("app.py",)))

        def callback(**kwargs):
            reviewed = run_review(**kwargs)
            if reviewed is None:
                return None
            provider_id, review = reviewed
            return provider_id, replace(review, identity=review.identity or identity)

        yield directory, callback
