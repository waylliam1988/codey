# 从原始工具回执直接交付精确 JSON 字段

当任务已经明确指定工具结果字段与答案字段的对应关系时，模型负责执行与未绑定内容，应用从已提交的完整回执组装绑定字段，然后交给原有完成门禁检查。

声明仍放在现有不可变 TaskPolicy.required_checks 中。公开 API、任务提交和 HeadlessRequest 的 required_tools 同时接受普通工具名与 require_tool_success 生成的规范声明；声明不会授予工具权限。

```python
import json

from codey.app.headless_runner import HeadlessRequest
from codey.policies.tool_requirement import require_tool_success

request = HeadlessRequest(
    project=None,
    task="读取浏览器脚本返回的记录。",
    intent="chat",
    requested_capabilities=("web.read", "browser.interact", "browser.effect"),
    output_format="json",
    output_schema=json.dumps({
        "type": "object",
        "properties": {"amount": {"type": "integer"}, "code": {"type": "string"}},
        "required": ["amount", "code"],
        "additionalProperties": False,
    }),
    required_tools=tuple(
        require_tool_success(
            "browser_eval",
            via="codemode",
            answer_pointer=f"/{key}",
            result_pointer=f"/value/{key}",
        )
        for key in ("amount", "code")
    ),
)
```

result_pointer 从完整回执的 canonical.value 内选择；browser_eval 的脚本返回值位于该对象的 value 字段。模型需要实际取得脚本结果，但 done.summary 可以只提交字符串 `{}`。应用从该结果生成最终 JSON，保留数字、布尔、空值、字符串、对象和数组的 JSON 类型。模型填写了绑定字段时，也以原始回执为准。未绑定的解释字段由模型提交并保留，最终仍须满足 output_schema。

answer_pointer 使用 JSON Pointer；空指针绑定整个答案，`~1` 和 `~0` 分别表示属性名中的 `/` 和 `~`。新建对象与数组的形状由输出 schema 决定；没有 schema 时，缺失容器按对象构造。数组中未绑定的前置元素需要模型在 JSON 提案中提供，应用不会凭空补值。

取值、参数匹配、父子执行关系和冲突判断共用同一个 ToolObservation。交付与门禁复用本次观测，不重新调用工具或读取文件。多个成功回执在同一选择器上不一致时继续拒绝完成；缺少完整回执时也不能使用展示文字或模型答案替代。多个目标绑定相同或重叠时必须一致，冲突会作为合同错误结束。

应用在现有内核完成步骤中产生临时投影，接受的结果通过现有 DoneDecision 持久化。原模型提交和原始工具回执保持各自原有身份；没有新增映射账本、发布写入或恢复运行时。Research 继续交付其原始 Markdown 提交，并沿用当前引用和发布合同；此 JSON 投影用于普通 done.summary 交付，不会重写 Research 报告。

## 从页面正文绑定精确字符串

已知页面的字段有固定文字边界时，可以在 `json_explanation` 任务里声明 `require_browser_read_field`。例如：

```python
from codey.policies.tool_requirement import require_browser_read_field

required_tools = (
    require_browser_read_field(
        url="https://example.com/records",
        prefix="记录校验码 ",
        suffix="。",
        answer_pointer="/code",
    ),
)
```

在 `output_schema` 中声明字符串字段 `code` 和 `explanation`。模型执行页面操作和 `browser_read`，并在 `done.summary` 提交说明。应用保留该说明，从指定当前文档最近一次匹配的已结算读取中填写 `code`。读取必须经成功的 CodeMode 父调用提交，正文必须完整，且只能有一个前缀。应用原样取出同一行内前缀与随后第一个后缀之间的字符串。重新导航后，旧文档的读取无法用于填字段。对于加载前后有两份记录的页面，前缀应明确标识需要报告的那份记录。
