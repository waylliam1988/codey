# 浏览器任务完成前置条件

将声明放入任务的 `required_tools`。声明随任务策略冻结，使用正式提交的工具回执和完成证明。任务仍需显式授予浏览器操作所需权限。

## Research 等待后读取并引用

```python
from codey.policies.browser_requirement import require_browser_research_sequence

required_tools = (
    require_browser_research_sequence(
        url="https://example.com/records",
        condition="text",
        value="Record loaded",
    ),
)
```

此检查用于 strict Research。完成时，要求在指定当前文档满足等待条件，随后在同一文档读取完整正文，并在 `submit_research_answer` 中引用该次正文。完成反馈会指出首个缺项。其他来源的有效引用可以共存。浏览器重载或切到同 URL 的其他标签时，主文档身份不同，旧等待和旧正文不能证明当前文档的要求。

## 普通 Chat 多轮页面操作

```python
from codey.policies.browser_requirement import require_browser_page_rounds

required_tools = (
    require_browser_page_rounds(
        url="https://example.com/records",
        rounds=2,
        fill_name="City",
        fill_value="Fuzhou",
        click_name="Load record",
        condition="text",
        value="Record loaded",
    ),
)
```

可声明 1 至 8 轮。每轮由一次导航创建独立主文档，之后按顺序取快照、填值、用新快照定位点击控件、点击、等待指定条件并读取完整正文。切页或重载使未完成的一轮失效，完成门会反馈下一项可执行操作。该声明只检查完成状态，任务仍由模型选择并执行后续浏览器操作。

## 生产链复核

真实 Chromium 和本地模型复核了两个提前提交场景。Research 拒绝旧引用，模型用一次请求更换成等待后的引用，未重复读取。页面任务拒绝只做一轮的提交，模型用两次请求补完第二轮并交付。两个完成证明都绑定了正式工具要求及其回执。实机产物位于 `.e2e-artifacts/browser-prerequisite-production-20261112/`。
