"""The grep tool has one rg backend and preserves exact structured observations."""
from __future__ import annotations

import pytest

from codey.policies.tool_requirement import require_tool_success
from codey.toolchain.runtime import search_files
from tests.test_required_tool_lineage import runtime as runtime


def test_rg_literal_regex_and_filter_have_explicit_contract(tmp_path):
    (tmp_path / "a.py").write_text("needle.*\nneedle123\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("needle999\n", encoding="utf-8")
    literal = search_files(tmp_path, ".", "needle.*")
    assert literal.ok
    assert [m["line"] for m in literal.canonical["value"]["matches"]] == [1]
    regex = search_files(tmp_path, ".", r"needle\d+", literal=False, include="*.py")
    assert regex.ok
    assert [(m["path"], m["line"], m["text"]) for m in regex.canonical["value"]["matches"]] == [
        ("a.py", 2, "needle123")]


def test_rg_empty_invalid_pattern_and_missing_binary_are_different(tmp_path, monkeypatch):
    from codey.toolchain import rg_search

    (tmp_path / "a.txt").write_text("known\n", encoding="utf-8")
    empty = search_files(tmp_path, ".", "absent")
    assert empty.ok and empty.canonical["value"]["matches"] == []
    invalid = search_files(tmp_path, ".", "[", literal=False)
    assert not invalid.ok and invalid.error_code == "search_invalid_pattern"
    monkeypatch.setattr(rg_search, "rg_binary", lambda: (_ for _ in ()).throw(FileNotFoundError("missing rg")))
    missing = search_files(tmp_path, ".", "known")
    assert not missing.ok and missing.error_code == "search_unavailable"


def test_paging_keeps_options_and_full_line_separate_from_display(tmp_path):
    text = "中文" * 200
    (tmp_path / "a.py").write_text((text + "\n") * 3, encoding="utf-8")
    page = search_files(tmp_path, ".", "中文", literal=False, case_sensitive=True, include="*.py", limit=1)
    assert page.ok and page.truncated
    value = page.canonical["value"]
    assert value["matches"][0]["text"] == text
    assert value["next_offset"] == 2
    assert '"literal":false' in page.model_text
    assert '"include":"*.py"' in page.model_text
    assert len(page.model_text) < len(text) + 300


@pytest.mark.parametrize("kwargs", [
    {"literal": "false"}, {"case_sensitive": 1}, {"include": "!*.py"},
    {"exclude": ["!*.py"]}, {"before_context": -1}, {"after_context": "bad"},
])
def test_invalid_options_are_rejected_before_spawn(tmp_path, kwargs):
    result = search_files(tmp_path, ".", "x", **kwargs)
    assert not result.ok


def test_hidden_ignored_and_host_config_do_not_change_admitted_scope(tmp_path, monkeypatch):
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("TOKEN\n", encoding="utf-8")
    (tmp_path / ".hidden.py").write_text("TOKEN\n", encoding="utf-8")
    excluded = tmp_path / "node_modules"
    excluded.mkdir()
    (excluded / "skip.py").write_text("TOKEN\n", encoding="utf-8")
    config = tmp_path / "host-config"
    config.write_text("--quiet\n", encoding="utf-8")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config))
    result = search_files(tmp_path, ".", "TOKEN", include="*.py")
    assert result.ok
    assert {m["path"] for m in result.canonical["value"]["matches"]} == {"ignored.py", ".hidden.py"}


def test_unicode_byte_columns_and_crlf_preserve_exact_line_text(tmp_path):
    (tmp_path / "a.py").write_bytes("  中文 TOKEN  \r\n".encode())
    result = search_files(tmp_path, ".", "TOKEN")
    assert result.ok
    assert result.canonical["value"]["matches"] == [
        {"path": "a.py", "line": 1, "column_byte": 10, "text": "  中文 TOKEN  "}]


@pytest.mark.parametrize("text", ["TOKEN\u2028中文\v😀", "TOKEN\r", "\ufeffTOKEN"])
def test_json_unicode_separators_and_raw_text_are_not_reinterpreted(tmp_path, text):
    (tmp_path / "a.py").write_bytes(text.encode("utf-8"))
    result = search_files(tmp_path, ".", "TOKEN")
    assert result.ok, result.model_text
    assert result.canonical["value"]["matches"][0]["text"] == text


def test_split_argv_batches_preserve_global_paging(tmp_path, monkeypatch):
    from codey.toolchain import rg_search

    for i in range(12):
        (tmp_path / f"{i:02}.py").write_text("TOKEN\n", encoding="utf-8")
    monkeypatch.setattr(rg_search, "ARGV_BYTES", 1000)
    first = search_files(tmp_path, ".", "TOKEN", limit=5)
    last = search_files(tmp_path, ".", "TOKEN", offset=11, limit=5)
    assert first.ok and last.ok
    assert [m["path"] for m in first.canonical["value"]["matches"]] == [f"{i:02}.py" for i in range(5)]
    assert [m["path"] for m in last.canonical["value"]["matches"]] == ["10.py", "11.py"]
    assert first.canonical["value"]["has_more"] and not last.canonical["value"]["has_more"]


@pytest.mark.parametrize("kind", ["missing-summary", "malformed-json", "unknown-event", "wrong-path", "raw-overflow",
                                  "summary-without-data", "summary-wrong-count", "status-mismatch"])
def test_incomplete_or_malformed_transport_never_becomes_empty_success(tmp_path, monkeypatch, kind):
    import json

    from codey.runtime.core.cancellation import CapturedProcess
    from codey.toolchain import rg_search

    (tmp_path / "a.py").write_text("TOKEN\n", encoding="utf-8")
    outputs = {"missing-summary": "", "malformed-json": "{", "unknown-event": '{"type":"other"}',
        "wrong-path": json.dumps({"type": "match", "data": {"path": {"text": "../outside.py"},
            "lines": {"text": "TOKEN\n"}, "line_number": 1, "submatches": [{"start": 0}]}}),
        "summary-without-data": '{"type":"summary"}',
        "summary-wrong-count": '{"type":"summary","data":{"stats":{"matched_lines":1}}}',
        "status-mismatch": '{"type":"summary","data":{"stats":{"matched_lines":0}}}'}
    raw = CapturedProcess([], 0, outputs.get(kind, ""), "", 0, 0, kind == "raw-overflow", False)
    monkeypatch.setattr(rg_search.cancellation, "run_process", lambda *_a, **_kw: raw)
    result = search_files(tmp_path, ".", "TOKEN")
    assert not result.ok
    assert result.error_code == ("search_output_limit" if kind == "raw-overflow" else "search_malformed_output")


def test_empty_admitted_scope_stays_bounded_and_incomplete_when_files_were_omitted(tmp_path, monkeypatch):
    from codey.toolchain import runtime

    assert search_files(tmp_path, ".", "TOKEN").ok
    (tmp_path / "a.py").write_text("TOKEN\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "SEARCH_MAX_FILE_BYTES", 1)
    result = search_files(tmp_path, ".", "TOKEN")
    assert result.ok and result.truncated
    assert result.canonical["value"]["matches"] == []
    assert not result.canonical["value"]["scan_complete"]


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("runtime", [(require_tool_success(
    "grep", via="codemode", arguments={"query": "TOKEN", "path": ".", "literal": False,
        "case_sensitive": True, "include": "*.py", "before_context": 1, "after_context": 1},
    answer_pointer="/hits", result_pointer="/matches"),)], indirect=True)
def test_real_codemode_search_preserves_full_receipt_and_recovery(runtime, native, monkeypatch):
    import json

    from codey.operations.completion_gate import evaluate
    from tests.test_required_tool_lineage import _code, _execute, _restart

    (runtime.project / "a.py").write_text("before\n  TOKEN exact  \n after\n", encoding="utf-8")
    result = _execute(runtime, [_code("const r = await tools.grep({query:'TOKEN',path:'.',literal:false,case_sensitive:true,include:'*.py',before_context:1,after_context:1});"
                                     "if (!r.ok) throw new Error(r.error); return r.value.matches;")], native)[0]
    assert result.ok, result.model_text
    expected = [{"path": "a.py", "line": 2, "column_byte": 3, "text": "  TOKEN exact  "}]
    assert result.canonical["value"] == expected
    answer = json.dumps({"hits": expected})
    assert evaluate(runtime.state, answer).complete
    assert not evaluate(runtime.state, '{"hits":[]}').complete
    restored = _restart(runtime)
    monkeypatch.setattr("codey.toolchain.rg_search.rg_binary", lambda: pytest.fail("recovery reexecuted rg"))
    assert evaluate(restored, answer).complete
    assert not evaluate(restored, '{"hits":[]}').complete


def test_stop_joins_the_existing_process_owner_and_closes_pipes(tmp_path, monkeypatch):
    import sys
    import threading

    from codey.runtime.core import cancellation

    (tmp_path / "a.py").write_text("TOKEN\n", encoding="utf-8")
    original = cancellation.start_process
    started = threading.Event()
    stop = threading.Event()
    processes, failures = [], []

    def start(_args, **kwargs):
        proc, owner = original([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        processes.append(proc)
        started.set()
        return proc, owner

    def search():
        try:
            with cancellation.scope(stop):
                search_files(tmp_path, ".", "TOKEN")
        except cancellation.TaskCancelled as exc:
            failures.append(exc)

    monkeypatch.setattr(cancellation, "start_process", start)
    thread = threading.Thread(target=search)
    thread.start()
    try:
        assert started.wait(5)
        stop.set()
        thread.join(5)
        assert not thread.is_alive() and failures
        assert processes[0].poll() is not None
        assert processes[0].stdout.closed and processes[0].stderr.closed
    finally:
        stop.set()
        thread.join(5)


def test_pending_search_intent_preserves_every_canonical_option(tmp_path):
    from codey.operations.task_effects import KernelEffectSink
    from codey.runtime.core.models import ToolCall
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.effects.safe_tool_replay import candidate_from_intent
    from tests.durable_runtime import accept_writer, open_runtime

    durable, mutations = open_runtime(tmp_path, session_id="s1", run_id="r1")
    try:
        accept_writer(durable, mutations, session_id="s1", run_id="r1")
        sink = KernelEffectSink(mutations, session_id="s1", run_id="r1", provider_id="local", durable=durable)
        args = {"path": ".", "query": "TOKEN-[0-9]+", "literal": False, "case_sensitive": True,
                "include": "*.py", "offset": 3, "limit": 2}
        sink.begin_turn([("effect-1", ToolCall("grep", args), 0)], turn=1)
        intent = RuntimeEffectStore(durable=durable).load_effects("s1", "r1")[0].intent
        assert intent.replay_args == args
        candidate = candidate_from_intent(intent)
        assert candidate is not None and candidate.call.args == args
    finally:
        durable.close()


def test_pending_search_intent_preserves_glob_context_options(tmp_path):
    from codey.operations.task_effects import KernelEffectSink
    from codey.runtime.core.models import ToolCall
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.effects.safe_tool_replay import candidate_from_intent
    from tests.durable_runtime import accept_writer, open_runtime

    durable, mutations = open_runtime(tmp_path, session_id="s1", run_id="r1")
    try:
        accept_writer(durable, mutations, session_id="s1", run_id="r1")
        sink = KernelEffectSink(mutations, session_id="s1", run_id="r1", provider_id="local", durable=durable)
        args = {"path": ".", "query": "TOKEN", "include": "*.py", "exclude": ["vendor/**"],
                "before_context": 2, "after_context": 3, "offset": 2, "limit": 4}
        sink.begin_turn([("effect-1", ToolCall("grep", args), 0)], turn=1)
        intent = RuntimeEffectStore(durable=durable).load_effects("s1", "r1")[0].intent
        assert intent.replay_args == args
        candidate = candidate_from_intent(intent)
        assert candidate is not None and candidate.call.args == args
    finally:
        durable.close()


def test_packaged_binary_works_without_system_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    (tmp_path / "space 中文.py").write_text("--literal-TOKEN\n", encoding="utf-8")
    result = search_files(tmp_path, ".", "--literal-TOKEN")
    assert result.ok, result.model_text
    assert result.canonical["value"]["matches"][0]["path"] == "space 中文.py"


def test_timeout_uses_existing_process_teardown_and_is_not_empty_success(tmp_path, monkeypatch):
    import sys

    from codey.runtime.core import cancellation
    from codey.toolchain import rg_search

    original = cancellation.start_process
    processes = []

    def start(_args, **kwargs):
        proc, owner = original([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        processes.append(proc)
        return proc, owner

    (tmp_path / "a.py").write_text("TOKEN\n", encoding="utf-8")
    monkeypatch.setattr(cancellation, "start_process", start)
    monkeypatch.setattr(rg_search, "SEARCH_TIMEOUT_SECONDS", 0.1)
    result = search_files(tmp_path, ".", "TOKEN")
    assert not result.ok and result.error_code == "search_timeout"
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed








def test_glob_preserves_default_excluded_directories(tmp_path):
    (tmp_path / "visible.py").write_text("needle\n", encoding="utf-8")
    hidden = tmp_path / "node_modules"
    hidden.mkdir()
    (hidden / "hidden.py").write_text("needle\n", encoding="utf-8")

    result = search_files(tmp_path, ".", "needle", include="*.py")

    assert result.ok
    assert [row["path"] for row in result.canonical["value"]["matches"]] == ["visible.py"]




def test_glob_respects_discovery_budget_before_search(tmp_path):
    from unittest import mock

    import codey.toolchain.runtime as tool_runtime

    for index in range(80):
        (tmp_path / f"file-{index:03}.py").write_text("needle\n" * 32, encoding="utf-8")

    with mock.patch.object(tool_runtime, "SEARCH_MAX_SCAN_FILES", 3):
        result = tool_runtime.search_files(tmp_path, ".", "needle", include="*.py")

    assert result.ok
    value = result.canonical["value"]
    assert value["coverage"]["budget_limited"]
    assert value["coverage"]["files_admitted"] < 80
    assert value["scan_complete"] is False


def test_context_parser_rejects_malformed_and_non_utf8_events(tmp_path):
    import json

    from codey.toolchain.rg_search import SearchError, _parse_events

    source = tmp_path / "a.py"
    source.write_text("needle\n", encoding="utf-8")
    malformed = json.dumps({"type": "context", "data": {"path": {"text": str(source)},
        "lines": {"text": "before\n"}, "line_number": "1"}})
    try:
        _parse_events(malformed + "\n", tmp_path, None)
    except SearchError as exc:
        assert exc.code == "search_malformed_output"
    else:
        raise AssertionError("malformed context line was accepted")

    binary = json.dumps({"type": "match", "data": {"path": {"text": str(source)},
        "lines": {"bytes": "AP8="}, "line_number": 1, "submatches": []}})
    try:
        _parse_events(binary + "\n", tmp_path, None)
    except SearchError as exc:
        assert exc.code == "search_invalid_encoding"
    else:
        raise AssertionError("non-UTF-8 context was accepted")


def test_asymmetric_context_does_not_leak_from_previous_page(tmp_path):
    (tmp_path / 'a.py').write_text(''.join('needle\n' if n in (3, 8) else f'line{n}\n' for n in range(1, 14)))
    result = search_files(tmp_path, '.', 'needle', offset=2, limit=1, before_context=0, after_context=4)
    assert result.ok, result.model_text
    value = result.canonical['value']
    assert [r['line'] for r in value['matches']] == [8]
    assert [(r['line'], r['relation']) for r in value['context']] == [(n, 'after') for n in (9, 10, 11, 12)]


def test_glob_does_not_enumerate_unrelated_subtrees(tmp_path, monkeypatch):
    from codey.toolchain import rg_search
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'a.py').write_text('needle')
    ignored = tmp_path / '.git'
    ignored.mkdir()
    for n in range(70):
        (ignored / (str(n) + 'x' * 120 + '.py')).write_text('needle')
    monkeypatch.setattr(rg_search, 'SEARCH_RAW_BYTES', 4096)
    result = search_files(tmp_path, 'src', 'needle', include='*.{py,ts}')
    assert result.ok, result.model_text
    assert [r['path'] for r in result.canonical['value']['matches']] == ['src/a.py']


def test_glob_filter_runs_before_content_byte_admission(tmp_path, monkeypatch):
    import codey.toolchain.runtime as runtime
    (tmp_path / 'a.txt').write_text('x' * 500)
    (tmp_path / 'b.py').write_text('needle')
    monkeypatch.setattr(runtime, 'SEARCH_MAX_SCAN_BYTES', 10)
    result = search_files(tmp_path, '.', 'needle', include='*.py')
    assert result.ok, result.model_text
    assert result.canonical['value']['scan_complete']
    assert result.canonical['value']['matches'][0]['path'] == 'b.py'


def test_literal_query_whitespace_is_preserved(tmp_path):
    (tmp_path / 'a.py').write_text('needle\n needle \n')
    result = search_files(tmp_path, '.', ' needle ')
    assert result.ok, result.model_text
    value = result.canonical['value']
    assert value['query'] == ' needle '
    assert [row['line'] for row in value['matches']] == [2]


def test_adjacent_hit_outside_page_is_available_as_context(tmp_path):
    (tmp_path / 'a.py').write_text('needle1\nneedle2\nlast\n')
    result = search_files(tmp_path, '.', 'needle', limit=1, after_context=2)
    assert result.ok, result.model_text
    value = result.canonical['value']
    assert [r['line'] for r in value['matches']] == [1]
    assert [(r['line'],r['text']) for r in value['context']] == [(2,'needle2'),(3,'last')]
    assert value['has_more']


def test_glob_never_reenumerates_a_directory_with_unknown_entry_coverage(tmp_path, monkeypatch):
    from codey.toolchain import rg_search, runtime
    (tmp_path / 'a.py').write_text('needle')
    (tmp_path / 'b.py').write_text('needle')
    monkeypatch.setattr(runtime, 'SEARCH_MAX_DIR_ENTRIES', 1)
    monkeypatch.setattr(rg_search.RgExecutor, '_run', lambda *a,**k: (_ for _ in ()).throw(AssertionError('walked beyond discovery budget')))
    result = search_files(tmp_path, '.', 'needle', include='*.py')
    assert not result.ok and result.error_code == 'search_glob_discovery_limit'


def test_empty_scope_still_reports_invalid_native_patterns(tmp_path):
    for options in ({'literal':False}, {'include':'[unterminated'}):
        result = search_files(tmp_path, '.', '[', **options)
        assert not result.ok and result.error_code == 'search_invalid_pattern'
    valid = search_files(tmp_path, '.', 'needle', include='*.{py,ts}')
    assert valid.ok and valid.canonical['value']['matches'] == []
