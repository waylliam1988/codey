"""New chats retain the selected API model instead of catalog's first row."""
from playwright.sync_api import expect

from tests.test_ui_workflow import page, select_connection_models, ui_browser  # noqa: F401 -- real browser fixtures


def test_new_chat_copies_api_choice_without_sharing_mutable_effort_map(page):  # noqa: F811
    select_connection_models(page, {"id": "zen", "label": "OpenCode Zen", "models": [
        {"id": "first-model", "name": "First", "efforts": []},
        {"id": "selected-model", "name": "Selected", "efforts": ["low", "high"]},
    ]})
    page.evaluate("""() => {
      CodeyProviderUI.applyConfig({default:'deepseek', providers:[{id:'zen',label:'OpenCode Zen'}]});
      CodeyProviderUI.applyApiModels([{id:'zen',models:[
        {id:'first-model',name:'First',efforts:[]},
        {id:'selected-model',name:'Selected',efforts:['low','high']}]}]);
      const s=activeSession(); s.provider='zen';
      s.modelSelection={connection_id:'zen',model:'selected-model',effort:'low',efforts:{'selected-model':'low'}};
      renderChat();
    }""")
    page.locator("#btn-new-chat").click()
    expect(page.locator("#provider-name")).to_have_text("Selected")
    expect(page.locator("#effort-name")).to_have_text("Low")
    result = page.evaluate("""() => {
      const now=activeSession(), old=sessions.find(s=>s.id==='a');
      now.modelSelection.efforts['selected-model']='high';
      return {provider:now.provider,model:now.modelSelection.model,original:old.modelSelection.efforts['selected-model']};
    }""")
    assert result == {"provider": "zen", "model": "selected-model", "original": "low"}
