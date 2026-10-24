"""The shipped renderer presents proof refs separately from the answer body."""
from playwright.sync_api import expect

from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def test_shipped_receipt_renders_proof_basis_without_modifying_fixed_json(page):
    answer = '{"ok":true}'
    rendered = page.evaluate("""answer => {
      const receipt = {display: {summary: 'Completed', evidence_refs: ['model claim']},
        completion_proof: {evidence_refs: ['source:verified'], limitation_refs: ['limit:observed']}};
      const text = Render.receiptSummary(receipt);
      addToSession('a', {type: 'asst', text: answer});
      addToSession('a', {type: 'done', text});
      return text;
    }""", answer)
    assert rendered == "Completed\n依据：source:verified\n限制：limit:observed"
    assert page.locator(".msg.asst .body").text_content().strip() == answer
    expect(page.locator("#chat-area")).to_contain_text("source:verified")
    expect(page.locator("#chat-area")).to_contain_text("limit:observed")
    expect(page.locator("#chat-area")).not_to_contain_text("model claim")
