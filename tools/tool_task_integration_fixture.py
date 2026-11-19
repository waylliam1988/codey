"""An isolated checkout service for a real model coding/lifecycle exercise."""
from __future__ import annotations

from pathlib import Path

FILES: dict[str, str] = {
    "pytest.ini": "[pytest]\ntestpaths = .\n",
    "README.md": '''# Checkout service release repair

This stdlib Python service stores orders and inventory in SQLite and exposes
POST /orders over HTTP. Money is integer cents. No external dependencies except pytest.

Reported defects: mobile retries debit stock repeatedly, invalid quantities can
increase inventory, and concurrent identical requests must create one order.
Repair checkout.py without changing the public API, tests, or database schema.
The same idempotency key with a different payload must raise ValueError.
An invalid order must leave inventory unchanged. Connections must close.

Commands:
- python -m pytest -q -s test_checkout.py (unit, concurrent SQLite, real HTTP, partner integration)
- python -m pytest -q -s probe_partner.py (150 second partner diagnostic; cancel after partner-ready)
- python -m pytest -q test_health.py (independent quick check)

probe_partner.py is deliberately outside normal test discovery.
''',
    "checkout.py": '''from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        with closing(self.connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS stock (sku TEXT PRIMARY KEY, units INTEGER, cents INTEGER)")
            db.execute("CREATE TABLE IF NOT EXISTS orders (request_key TEXT PRIMARY KEY, sku TEXT, quantity INTEGER, total INTEGER)")
            db.execute("INSERT OR IGNORE INTO stock VALUES ('coffee', 20, 1250)")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def remaining(self) -> int:
        with closing(self.connect()) as db:
            return int(db.execute("SELECT units FROM stock WHERE sku='coffee'").fetchone()[0])

    def place_order(self, request_key: str, sku: str, quantity: int) -> dict[str, int | str]:
        with closing(self.connect()) as db, db:
            stock = db.execute("SELECT units, cents FROM stock WHERE sku=?", (sku,)).fetchone()
            if stock is None or stock['units'] < quantity:
                raise ValueError('insufficient stock')
            db.execute("UPDATE stock SET units=units-? WHERE sku=?", (quantity, sku))
            previous = db.execute("SELECT * FROM orders WHERE request_key=?", (request_key,)).fetchone()
            if previous is not None:
                return dict(previous)
            total = int(stock['cents']) * quantity
            db.execute("INSERT INTO orders VALUES (?, ?, ?, ?)", (request_key, sku, quantity, total))
            return {'request_key': request_key, 'sku': sku, 'quantity': quantity, 'total': total}
''',
    "api.py": '''from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from checkout import Store


def make_server(store: Store) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            status = 200
            try:
                payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
                body = store.place_order(payload['request_key'], payload['sku'], payload['quantity'])
            except (ValueError, KeyError, TypeError) as exc:
                status = 409
                body = {'error': str(exc)}
            encoded = json.dumps(body).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return ThreadingHTTPServer(('127.0.0.1', 0), Handler)
''',
    "test_checkout.py": '''import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from api import make_server
from checkout import Store


def test_retry_does_not_debit_twice(tmp_path):
    store = Store(tmp_path / 'orders.sqlite3')
    first = store.place_order('mobile-1', 'coffee', 2)
    assert store.place_order('mobile-1', 'coffee', 2) == first
    assert first['total'] == 2500
    assert store.remaining() == 18


@pytest.mark.parametrize('quantity', [-1, 0, True])
def test_invalid_quantity_is_atomic(tmp_path, quantity):
    store = Store(tmp_path / 'orders.sqlite3')
    with pytest.raises(ValueError):
        store.place_order('invalid', 'coffee', quantity)
    assert store.remaining() == 20


def test_reused_key_with_other_payload_is_rejected(tmp_path):
    store = Store(tmp_path / 'orders.sqlite3')
    store.place_order('same', 'coffee', 2)
    with pytest.raises(ValueError):
        store.place_order('same', 'coffee', 3)
    assert store.remaining() == 18


def test_out_of_stock_is_atomic(tmp_path):
    store = Store(tmp_path / 'orders.sqlite3')
    with pytest.raises(ValueError):
        store.place_order('large', 'coffee', 100)
    assert store.remaining() == 20


def test_concurrent_mobile_retries(tmp_path):
    store = Store(tmp_path / 'orders.sqlite3')
    barrier = threading.Barrier(4)
    def request(_index):
        barrier.wait(timeout=5)
        return store.place_order('concurrent', 'coffee', 2)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(request, range(4)))
    assert all(row == results[0] for row in results)
    assert store.remaining() == 18


def test_http_retry_and_conflict(tmp_path):
    store = Store(tmp_path / 'orders.sqlite3')
    server = make_server(store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def post(quantity):
        data = json.dumps({'request_key': 'http-1', 'sku': 'coffee', 'quantity': quantity}).encode()
        with urlopen(Request(f'http://127.0.0.1:{server.server_port}/orders', data=data,
                             headers={'Content-Type': 'application/json'}), timeout=5) as response:
            return json.load(response)
    try:
        first = post(2)
        assert post(2) == first
        with pytest.raises(HTTPError) as error:
            post(3)
        assert error.value.code == 409
        assert store.remaining() == 18
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_partner_roundtrip():
    print('partner-integration-start', flush=True)
    time.sleep(12)
    print('partner-integration-finished', flush=True)
''',
    "probe_partner.py": '''import time


def test_partner_diagnostic():
    print('partner-ready', flush=True)
    for index in range(150):
        print(f'partner-heartbeat-{index}', flush=True)
        time.sleep(1)
''',
    "test_health.py": '''from checkout import Store


def test_health(tmp_path):
    store = Store(tmp_path / 'health.sqlite3')
    assert store.remaining() == 20
    assert store.place_order('health', 'coffee', 1)['total'] == 1250
''',
}

TASK = (
    "Repair this SQLite checkout service. Change only checkout.py; preserve tests, API and database schema. "
    "The required behavior is: quantity must be a positive integer (Python bool is invalid); retrying an identical "
    "request returns the original order without another stock debit; reusing a key with a different payload raises "
    "ValueError; concurrent identical requests must create one order and debit stock once; failed requests leave "
    "stock unchanged; connections close. Follow these phases:\n"
    "1. Call run(command=\"python -m pytest -q -s test_checkout.py\", run_in_background=true). "
    "Before the first edit, read README.md, checkout.py and test_checkout.py, and use task_output to read the "
    "formal failing result. The accepted receipt is not the result.\n"
    "2. Repair the defects found in code and tests. Repeat the same run with run_in_background=true and read "
    "its final result with task_output. If it fails, inspect and repair the remaining defects before running again. "
    "Do not move to phase 3 until the regression actually passes.\n"
    "3. Call run(command=\"python -m pytest -q -s probe_partner.py\", run_in_background=true). "
    "Read live output with task_output. Once partner-ready appears, use task_cancel and confirm cancelled.\n"
    "4. Run python -m pytest -q test_health.py in the foreground. Keep the main task running after cancelling "
    "the diagnostic. Report the defects, changes, initial failure, fixed results and cancelled diagnostic honestly."
)


def create(project: Path) -> str:
    for name, contents in FILES.items():
        (project / name).write_text(contents, encoding="utf-8")
    return TASK
