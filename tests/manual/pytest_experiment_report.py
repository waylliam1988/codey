"""Persist deterministic experiment results without production monkeypatches."""
import hashlib
import json
from pathlib import Path


class ReportPlugin:
    def __init__(self, report):
        self.path, self.results = report, []

    def pytest_runtest_logreport(self, report):
        if report.when == 'call' or (report.when == 'setup' and report.failed):
            self.results.append({'test': report.nodeid, 'phase': report.when, 'outcome': report.outcome,
                                 'observations': dict(report.user_properties).get('observations'),
                                 'failure': str(report.longrepr) if report.failed else ''})

    def pytest_sessionfinish(self, session, exitstatus):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        script = Path(__file__)
        self.path.write_text(json.dumps({'exit_code': int(exitstatus),
            'script_sha256': hashlib.sha256(script.read_bytes()).hexdigest(),
            'test_only_proposal_applied': False, 'production_files_written': False, 'model_calls': 0,
            'limitations': ['Scripted choices; no claim about real-model reliability.',
                            'No production proposal is installed.'],
            'results': self.results}, ensure_ascii=False, indent=2), encoding='utf-8')
