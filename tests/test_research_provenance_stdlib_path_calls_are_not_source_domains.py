"""Actual pathlib report excerpts distinguish stdlib calls from unopened URLs."""
import pytest

from codey.research.provenance import provenance_problem


@pytest.mark.parametrize("api", ["os.path.join", "Path.walk", "os.scandir", "Path.resolve",
    "os.path.abspath", "os.path.relpath", "PurePath.anchor", "Path.absolute", "pathlib.UnsupportedOperation", "os.PathLike",
    "Path.move", "Path.info"])
@pytest.mark.parametrize("formatting", ["{api}()", "`{api}()`"])
def test_observed_pathlib_report_apis_do_not_claim_external_source_domains(api, formatting):
    summary = "The official documentation describes " + formatting.format(api=api) + "."
    assert provenance_problem(summary, opened_sources={"https://docs.python.org/3/library/pathlib.html"},
                              search_result_urls=set()) is None


@pytest.mark.parametrize("source", ["https://os.path.join/page", "https://path.walk/page",
                                   "https://os.scandir/page", "https://path.resolve/page", "unopened.example.org",
                                   "https://os.path.abspath/page", "https://os.path.relpath/page",
                                   "https://purepath.anchor/page", "https://path.absolute/page",
                                   "https://pathlib.unsupportedoperation/page", "https://os.pathlike/page",
                                   "https://path.move/page", "https://path.info/page"])
def test_real_urls_and_other_domains_are_still_rejected(source):
    assert provenance_problem("Source: " + source, opened_sources={"https://docs.python.org/3/library/pathlib.html"},
                              search_result_urls=set()) is not None
