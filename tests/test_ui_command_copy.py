"""Command and output copies share code-block icon sizing and independent reveal regions."""
from playwright.sync_api import expect

from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def prepare(page):
    page.evaluate("""() => {
        addToSession('a',{type:'user',text:'Inspect command output'});
        addToSession('a',{type:'asst',text:'```bash\\necho example\\n```'});
        addToSession('a',{type:'shell_result',approved:true,shellStatus:'exit',exitCode:0,
            command:'echo example',output:'example\\n'});
    }""")
    page.locator('.command-summary').click()


def test_command_and_output_copy_reveal_only_in_their_own_hover_region(page):
    prepare(page)
    command = page.get_by_role('button', name='Copy command', exact=True)
    output = page.get_by_role('button', name='Copy output', exact=True)
    page.locator('#task').hover()
    expect(command).to_have_css('opacity', '0')
    expect(output).to_have_css('opacity', '0')
    page.locator('.command-source pre').hover()
    expect(command).to_have_css('opacity', '1')
    expect(output).to_have_css('opacity', '0')
    command.hover()
    expect(command).to_have_css('pointer-events', 'auto')
    expect(output).to_have_css('opacity', '0')
    page.locator('.command-result pre').first.hover()
    expect(output).to_have_css('opacity', '1')
    expect(command).to_have_css('opacity', '0')
    page.locator('.command-panel-label').hover()
    expect(command).to_have_css('opacity', '0')
    expect(output).to_have_css('opacity', '0')


def test_copy_vectors_and_rendered_sizes_match_code_blocks(page):
    prepare(page)
    icons = page.locator('.code-copy svg,.command-source .icon-action svg,.command-result .icon-action svg')
    expect(icons).to_have_count(3)
    shapes = icons.evaluate_all("""nodes => nodes.map(e => ({
        viewBox:e.getAttribute('viewBox'), body:e.innerHTML,
        width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height,
        fill:getComputedStyle(e).fill,stroke:getComputedStyle(e).stroke
    }))""")
    assert all(shape['viewBox'] == shapes[0]['viewBox'] and shape['body'] == shapes[0]['body'] for shape in shapes)
    assert all(shape['width'] == 16 and shape['height'] == 16 for shape in shapes)


def test_keyboard_reveals_only_the_focused_copy_region(page):
    prepare(page)
    command = page.get_by_role('button', name='Copy command', exact=True)
    output = page.get_by_role('button', name='Copy output', exact=True)
    page.locator('#task').hover()
    page.locator('.command-summary').focus()
    page.keyboard.press('Tab')
    expect(command).to_be_focused()
    expect(command).to_have_css('opacity', '1')
    expect(output).to_have_css('opacity', '0')
    command.press('Tab')
    expect(output).to_have_css('opacity', '1')
    expect(command).to_have_css('opacity', '0')
