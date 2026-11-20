"""The experiment must distinguish API success from the full original requirements."""
import subprocess
import sys
from pathlib import Path

import pytest

from tools.tool_task_integration_fixture import FILES, create

CORRECT_METHOD = '''    def place_order(self, request_key: str, sku: str, quantity: int) -> dict[str, int | str]:
        if type(quantity) is not int or quantity <= 0:
            raise ValueError('invalid quantity')
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM orders WHERE request_key=?', (request_key,)).fetchone()
            if previous is not None:
                if previous['sku'] != sku or previous['quantity'] != quantity:
                    raise ValueError('conflicting payload')
                return dict(previous)
            stock = db.execute('SELECT units, cents FROM stock WHERE sku=?', (sku,)).fetchone()
            if stock is None or stock['units'] < quantity:
                raise ValueError('insufficient stock')
            total = int(stock['cents']) * quantity
            db.execute('UPDATE stock SET units=units-? WHERE sku=?', (quantity, sku))
            db.execute('INSERT INTO orders VALUES (?, ?, ?, ?)', (request_key, sku, quantity, total))
            return {'request_key': request_key, 'sku': sku, 'quantity': quantity, 'total': total}
'''


@pytest.mark.parametrize("candidate,expected", [("initial", "1 failed, 1 passed"),
                                               ("correct", "2 passed"),
                                               ("leaking", "1 failed, 1 passed")])
def test_boundary_acceptance_rejects_incomplete_repairs(tmp_path: Path, candidate: str, expected: str) -> None:
    create(tmp_path)
    source = FILES["checkout.py"]
    if candidate != "initial":
        source = source[:source.index("    def place_order(")] + CORRECT_METHOD
    if candidate == "leaking":
        source = source[:source.index("    def place_order(")] + CORRECT_METHOD.replace(
            "with closing(self.connect()) as db, db:", "with self.connect() as db:")
    (tmp_path / "checkout.py").write_text(source, encoding="utf-8")
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "test_checkout.py",
        "-k", "identical_retry_after_stock_is_exhausted or successful_order_closes_its_connection"],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", check=False, timeout=30)
    assert expected in result.stdout, result.stdout + result.stderr
    assert result.returncode == (0 if candidate == "correct" else 1)
