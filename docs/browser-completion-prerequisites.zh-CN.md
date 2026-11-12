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
