"""Minimal interface alone, or paired with lossless evidence-last ordering."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.support import research_partial_edit_candidate as partial

_partial_treatment = partial.treatment


@contextmanager
def treatment(arm):
    from codey.research import answer_context

    if arm in {"minimal_edit", "minimal_focus"}:
        original = answer_context._render_context

        def focused(*args):
            return partial.evidence_last(original(*args))

        selected = focused if arm == "minimal_focus" else original
        with _partial_treatment("partial_edit"), patch.object(answer_context, "_render_context", selected):
            yield
    else:
        with _partial_treatment(arm):
            yield


def main():
    with patch.object(partial, "treatment", treatment), \
            patch.object(partial, "__spec__", SimpleNamespace(name=__spec__.name)), \
            patch.object(partial, "__file__", str(Path(__file__).resolve())):
        partial.main()


if __name__ == "__main__":
    main()
