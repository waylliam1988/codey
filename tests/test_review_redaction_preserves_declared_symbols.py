"""Declared code symbols are context; credential values still need redaction."""
import pytest

from codey.reviews.input import prepare_review_input


def prepare(text):
    return prepare_review_input(project="p", task="Review", writer_summary="Done", changes={
        "ok": True, "files": [{"path": "pricing.py", "status": "M"}],
        "diff": "--- a/pricing.py\n+++ b/pricing.py\n@@ -1 +1 @@\n+return 1\n",
    }, project_map=text)


@pytest.mark.parametrize("symbol", [
    "def PricingTests.test_discount(self)",
    "def PaymentTests.test_refund(self)",
    "class CustomerAccountTestSuite",
    "function CustomerAccount_handler(customer)",
])
def test_declared_symbol_survives_review_input_without_partial_scope(symbol):
    text = "- tests/test_pricing.py: " + symbol
    result = prepare(text)
    assert text in result.prompt
    assert result.scope.is_complete


@pytest.mark.parametrize("text, secret", [
    ('api_key = "aB3dE5gH7jK9mN1pQ2rS4tU6"', "aB3dE5gH7jK9mN1pQ2rS4tU6"),
    ("def sk-Ab3dE5gH7jK9mN1pQ2rS4tU6(self)", "sk-Ab3dE5gH7jK9mN1pQ2rS4tU6"),
    ('def PricingTests.test_discount(self); api_key="aB3dE5gH7jK9mN1pQ2rS4tU6"', "aB3dE5gH7jK9mN1pQ2rS4tU6"),
    ('api_key="def aB3dE5gH7jK9mN1pQ2rS4tU6"', "aB3dE5gH7jK9mN1pQ2rS4tU6"),
])
def test_declared_symbol_context_does_not_exempt_secret_values(text, secret):
    result = prepare(text)
    assert secret not in result.prompt
    assert result.scope.content_redacted
    assert not result.scope.is_complete
