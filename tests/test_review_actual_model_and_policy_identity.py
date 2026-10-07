"""Review identity follows the actual model/configuration and routing policy."""
from types import SimpleNamespace

from codey.app.review_service import run_review_attempt
from codey.providers.api_provider import ApiProvider
from codey.reviews.identity import identities_match, scope_digest_for
from codey.reviews.input import ReviewScope


class LocalReviewer(ApiProvider):
    def __init__(self, model="model-a", base_url="http://localhost:5001/v1"):
        super().__init__(base_url, model)
        self.sent = []

    def send(self, text, timeout=None):
        self.sent.append(text)
        return '{"verdict":"approved","findings":[]}'


def attempt(tmp_path, reviewer, **kwargs):
    (tmp_path / "app.py").write_text("x", encoding="utf-8")
    return run_review_attempt(SimpleNamespace(state_home=None, emit=lambda row: None),
        session_id="s", project=str(tmp_path), task="review", writer_summary="done",
        changes={"ok": True, "files": [{"path": "app.py"}], "changed_count": 1,
                 "diff": "diff --git a/app.py b/app.py\n+x\n"}, recent_log="", change_brief="",
        project_map="", verification_map="", review_impact_map="", execution_evidence="",
        reviewer_id="local", reviewer=reviewer, self_review=False, **kwargs)[1]


def test_different_models_on_same_provider_do_not_match(tmp_path):
    first = attempt(tmp_path, LocalReviewer("model-a"))
    second = attempt(tmp_path, LocalReviewer("model-b"))
    assert not identities_match(first.identity, second.identity)


def test_different_endpoints_with_same_model_do_not_match(tmp_path):
    first = attempt(tmp_path, LocalReviewer(base_url="http://localhost:5001/v1"))
    second = attempt(tmp_path, LocalReviewer(base_url="http://localhost:5002/v1"))
    assert not identities_match(first.identity, second.identity)


def test_web_provider_without_model_identity_remains_unknown(tmp_path):
    reviewer = SimpleNamespace(new_chat=lambda: None, close=lambda: None,
                               send=lambda *args, **kwargs: '{"verdict":"approved","findings":[]}')
    assert attempt(tmp_path, reviewer).identity.model_id == ""


def test_actual_requested_review_policy_is_preserved(tmp_path):
    result = attempt(tmp_path, LocalReviewer(), review_policy="require_web")
    assert result.identity.policy == "require_web"


def test_scope_digest_does_not_alias_comma_containing_filenames():
    first = ReviewScope(total_changed_files=2, provided_files=("a,b.py", "c.py"))
    second = ReviewScope(total_changed_files=2, provided_files=("a", "b.py,c.py"))
    assert scope_digest_for(first) != scope_digest_for(second)


def test_formal_review_service_reuses_without_new_chat_or_send(tmp_path):
    from unittest.mock import Mock, patch

    from codey.reviews.persistence import append_review_result_ledger
    from codey.runs.ledger import RunLedgerStore

    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("x", encoding="utf-8")
    state = tmp_path / "state"
    rows = []
    ctx = SimpleNamespace(state_home=state, emit=rows.append)
    reviewer = LocalReviewer()
    reviewer.new_chat = Mock()
    args = dict(session_id="s", project=str(project), task="review", writer_summary="done",
        changes={"ok": True, "files": [{"path": "app.py"}], "changed_count": 1, "diff": "+x"},
        recent_log="", change_brief="", project_map="", verification_map="", review_impact_map="",
        execution_evidence="", reviewer_id="local", reviewer=reviewer, self_review=False)
    with patch("codey.reviews.persistence.load_recorded_review", side_effect=AssertionError("default must not query history")):
        first = run_review_attempt(ctx, **args, run_id="r1")[1]
    assert first.is_complete and first.identity.artifact_sha256
    ledger = RunLedgerStore(state).open(run_id="r1", session_id="s", project=str(project), task="review", provider="local", mode="review")
    append_review_result_ledger(lambda action: action(ledger), first)
    ledger.finish(summary="done", stop_reason="done", turns=1, max_turns=1, provider="local")
    second = run_review_attempt(ctx, **args, run_id="r2", review_source_run_id="r1")[1]
    assert second.origin == "reused" and second.source_run_id == "r1"
    assert len(reviewer.sent) == 1
    assert reviewer.new_chat.call_count == 1
    assert rows[-1]["review"]["origin"] == "reused"
