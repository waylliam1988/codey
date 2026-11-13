# 结构化动作与说明约束验收

基线：`b30c087c`。本轮在原工作区修改，没有创建或复制 worktree，也没有运行全量 pytest。

## 改了什么

任务可以通过现有 `required_tools` 显式选择结构化动作：

```python
from codey.policies.tool_requirement import require_structured_actions

required_tools = (require_structured_actions(),)
```

这是一条现有 CodeMode 成功要求的扩展声明，不授予浏览器或文件权限。任务入口仍须声明需要的能力；需要强制完成某个工具时，继续添加原有工具成功要求。strict Research 不接受这个可选入口；普通 Chat 和 Hybrid 可以启用。没有启用的任务继续使用通用 CodeMode。

同一份冻结的工具定义生成 JSON 和 native 的 `action` 联合，再把动作参数降为固定 CodeMode 程序。权限、叶子参数校验、执行、文件版本检查和正式回执仍沿用原链，没有增加执行器或凭证来源。冻结权限不支持的动作不会公布。

模型可以这样提交：

```json
{"tool":"codemode","args":{"action":{"kind":"navigate","target":"task"}}}
```

```json
{"tool":"codemode","args":{"action":{"kind":"fill","target":{"role":"textbox","name":"Locality"},"value":"福州"}}}
```

```json
{"tool":"codemode","args":{"action":{"kind":"click","target":{"role":"button","name":"Load record"}}}}
```

```json
{"tool":"codemode","args":{"action":{"kind":"edit_json","path":"result.json","changes":{"status":"reviewed"}}}}
```

还提供按权限裁剪的 `snapshot`、`wait`、`read`、`eval`、`tabs` 和 `download`。模型不再为导航、控件查找或 JSON 编辑编写 JavaScript；`eval` 的页面表达式仍由任务指定或模型选择。

### URL 与恢复

Chat 和 Hybrid 在地址唯一、明确授权、原始写法无歧义时，都可以绑定冻结的任务 URL。结构化导航只接受 `target: "task"`，不接受模型重抄的 URL。三个否定写法的原提交复现见 `.e2e-artifacts/structured-actions-negation-reproduction.json`：基线都错误地要求访问目标，本轮都不再要求访问。

快照允许兼容的 action 绑定与任务 URL 共存；恢复时根据冻结叶子定义重建 action 规范并比较，同时核对导航权限。删除 URL 却保留导航分支、删除动作绑定等矛盾快照被拒绝。同版本恢复经过真实执行验证；跨版本仍受原有 implementation identity 约束，不承诺无缝恢复。

### 控件与投递状态

具名动作精确匹配快照中的角色、名称；范围是主框架可见控件，名称折叠空白并截至 240 字符。程序获取真实快照，只有唯一且控件列表未截断时才提取 ref。无匹配、同名或列表截断都会明确失败；同名时附候选 ref，模型可以提交 `target: {"ref": "从候选选出的引用"}`。

显式 ref 不会先自动刷新快照，也不会被悄悄替换。真正的旧 ref 继续走原来的过期检查。失败反馈保留叶子回执里的 `delivery`：未投递、已投递但观察失败、投递不确定分别处理，后两者要求先观察页面，避免盲目重试。

### JSON 编辑

固定程序先 `read_file`，先用 `unwrap` 报告读取本身的错误，再检查是否完整读取，最后把未解包的原始读取回执交给 `edit`。冲突后重交同一个结构化动作，会建立新的正式读取回执；没有将文件正文视为编辑凭证，也没有放宽版本检查。

这个动作只支持默认读取窗口内的完整 JSON 对象：默认 300 行、16,000 字符；超限明确拒绝且不投递编辑。只替换已有顶层字段，保留其他字段。变更值继续受 CodeMode JavaScript 安全整数限制；既有文件中未修改的精确数字仍由原 JSON 编辑实现保留。

### 说明中的元数据

独立字段的精确绑定新增显式 `omit_from_explanation=True` 选项。例如：

```python
require_tool_success(
    "browser_eval", via="codemode",
    arguments={"code": "window.__contractToken"},
    answer_pointer="/token", result_pointer="/value",
    omit_from_explanation=True,
)
```

选值仍由同一个 `ToolObservation` 负责字段组装和完成检查。说明中重复该值会被拒绝，保留原稿及选值、回执引用，让模型只修正 `done.summary`。明确要求同一选择器的值既出现在说明中又不得出现，会在任务入口拒绝。

URL 禁令由调用方在现有 `/explanation` Schema 上声明，例如 `\A(?![\s\S]*[Hh][Tt][Tt][Pp][Ss]?://)[\s\S]*\Z`。它也能拒绝把连字符抄成下划线的 `HTTPS://` 地址。程序不删除用户约束、不静默改写说明、不做全局换行替换。真实段落要求继续使用已有 Schema；路径、代码中的字面转义保持原样。

## 验证记录

结果在实机验收结束后补齐。全部首次失败及重跑日志保留在 `.e2e-artifacts/structured-actions-*` 和 `.e2e-artifacts/structured-actions-20261113/`。

## 剩余限制

这是显式启用的确定性动作入口，不是任意任务的自动规划器。多目标、来源歧义等情况不会猜测一个 URL。上传仍使用原专用合同或通用 CodeMode；已识别的专用上传、网页复制合同不能被 action 入口静默覆盖。

它降低了模型编写循环、抄写地址和搬运编辑凭证的负担，不能保证模型总能选对业务对象、正确理解事实或在有限轮次内纠错。说明检查只保证已声明选值的出现或排除；不证明任意自然语言语义，也不自动识别所有错误 token 变体。入口矛盾检查针对同一个显式选择器，不推断两个不同选择器在未来是否会偶然得到相同值。
