# 说明事实交付、输出格式和来源边界修复

日期：2026-11-13。基于 `890bec00`，只修改本轮生产路径、相关测试和既有实机工具的参数入口。未创建或复制 worktree，未运行全量 pytest。

## 修复结果与使用边界

### 来源与上传说明

三个审查问题均在修改前复现。`boundary-baseline.log` 为 **8 失败、64 通过**，失败对应三类问题的不同输入。

1. 同一规范化 URL 被否定后，搜索替代资料时提及该地址不会重新把它列为必需来源；之后明确请求打开、访问或读取时可以恢复。撤销和恢复继续只维护现有来源决定，不新增第二套授权状态，不改变已冻结任务的跨轮次合同。
2. 相邻 URL 行只关联尚未给出 URL 目标的打开指令。`Open only <A>.` 后的 `Source: <B>` 不再继承前一句动作；目标为空的指令仍支持连续裸地址与允许标签地址组成一组。预取与上传入口继续消费同一份来源要求。
3. 上传状态检查识别 `cannot confirm whether`、`cannot verify if` 等不确定性。紧接着另外断言成功仍会被拒绝。检查继续只读取组装 JSON 的 `explanation`，原说明及页面正文保持原样；不是任意自然语言的真实性证明。

### 显式的说明交付规则

在现有 `ToolSuccessRequirement` 增加可选 `answer_contains=True`，只允许目标 `/explanation`，并要求 `json_explanation` 输出。既有选择器继续确定正式回执、当前页面、结果字段或正文标记，新模式只检查说明，不参与字段组装。

例如，调用方可以声明从 `browser_eval` 正式结果的 `/value/count` 取值，并要求说明包含该值：

```python
require_tool_success(
    "browser_eval", via="codemode", arguments={"code": "window.result"},
    answer_pointer="/explanation", result_pointer="/value/count",
    answer_contains=True,
)
```

把该声明放入现有任务入口的 `required_tools`，同时声明 `output_format="json_explanation"`。正文中的明确文本可以继续使用 `require_browser_read_field(..., answer_contains=True)`。URL、token 等独立 JSON 字段保留原有精确绑定，不要求无条件在说明里重复。未启用新模式时，原声明及检查标识不变。

第一版支持非空文本和有限 JSON 数字。文本按完整字面值检查；ASCII 标识符不能靠较长标识符的子串满足。数字按完整 JSON 数字拼写匹配，`173` 不会被 `1173`、`1730`、`173.5`、`-173` 或 `173e2` 满足。相邻中文单位可以保留。

期望值来自本次正式回执，不在生产代码中列举净重、校验码或夹具答案。测试同时使用两组不同回执值，验证旧答案被拒绝。缺失、截断、不一致、选择歧义及类型不适用仍属于证据问题，不能靠编辑说明绕过。

这是明确值的出现检查，不证明任意语义正确，也不自动推断任务全文中的所有事实。例如含有正确数字不等于已经正确解释它；数字 `-2.5` 与 `-2.50` 在本模式下也不是相同字面拼写。调用方需要显式声明可核验的交付项。

### 换行与答案重试

真实换行及段落数由已有输出 JSON Schema 的 `/explanation` 字符串约束负责，校验 JSON 解码后的字符串。没有新增自然语言格式推断器，也不全局替换字面 `\n`。回归分别覆盖“至少两段”和“恰好两段”，接受 LF、CRLF，拒绝空白第二段和字面转义冒充分段，保留路径及代码中的转义。

事实漏交反馈包含选中值、原回执引用和解码后的原草稿；格式反馈包含具体约束、schema 和原草稿。证据已经满足时，明确要求保留其他分析，只修正说明并重交 `done`。复用既有完成拒绝、进展计数和重试机制，没有自动补写结论、重新读取或重选附件。

没有修改 ref、编辑凭证、版本冲突规则或它们的提示策略。

## 定向生产回归

最终定向套件 **381 项通过**，日志：`.e2e-artifacts/completion-contract-boundaries-20261113/production-regression-final.log`。范围包括说明合同、生产入口与真实 Chromium、来源识别和预取、导航绑定、上传说明与结构化上传、回执 lineage、浏览器报告、输出 schema 和来源覆盖。此前 **148 项**、**379 项**的阶段运行分别保留，有重叠，不累加为不同测试项。

新增固定回复生产链用例覆盖：

- 漏交事实被拒绝；补齐后仍用字面换行再次被拒绝；真实分段后通过，最终说明逐字保留。
- 更换回执值后，原答案不能通过；纠正期间只有一次正文读取、一次点击，经过快照序列化恢复。
- 被禁止的搜索参照及背景来源不会进入首轮预取；唯一应读来源的正式回执出现在首个模型请求中。
- 上传说明表达 `cannot confirm whether` 可以完成；格式修复只重交 `done`，全程只有一次附件选择、一次选择后读取。
- 证据缺失、截断和冲突不能误报为只需修改答案；旧精确字段组装及 `/explanation` 覆盖保护保持有效。

首轮新增生产测试 **2 失败、2 通过**，原始请求、提案、回执及结果保存在 `production-first-failures/0/`、`production-first-failures/1/`。这两项在最终断言中错误地把解码草稿与再次 JSON 编码的整个请求比较；前面的正式回执、两次拒绝、最终说明及工具次数断言均通过。核对原始请求后，测试改为检查解码后的 `messages[].content`，没有放宽生产合同。

测试进程通过后出现 RAM 临时目录清理 `WinError 3` 警告，退出码为 0，保留在原日志；不计作生产回归失败。

最终仅补充了既有测试替身的精确回调签名，其所在来源回归文件再次 **41 项通过**，与 381 项有重叠，日志为 `source-regression-after-callback.log`。

## 类型与静态检查

- Ruff：10 个修改的 Python 文件通过。
- 生产全树 strict mypy（Python 3.11）：680 个文件通过。
- 10 个修改 Python 文件的 strict mypy，并额外启用 `disallow_any_expr`：通过。
- 内核类型精度门通过；10 个修改文件的 AST 检查无 Any、cast、忽略检查、裸泛型或未限定回调签名。
- `git diff --check` 通过。

按本轮类型要求，也清除了修改文件原有的 JSON 解析边界、排序回调、字典去重及测试回调中的隐式动态类型。生产全树检查与修改文件增强检查分别记录，没有关闭检查或增加豁免。初次增强检查发现的问题及最终通过记录分开保留。

证据目录：`.e2e-artifacts/completion-contract-boundaries-20261113/`。静态日志为 `ruff-after-callback.log`、`mypy-production-final.log`、`mypy-precision-after-callback.log`、`kernel-type-precision-final.log`、`changed-type-precision.json`。

## 完整实机矩阵

使用既有 `tools.browser_full_surface_live_gate`，真实 KoboldCpp Gemma4 模型与 Chromium，通过生产 Registry、入口、CodeMode、完成检查和正式回执链。模型为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，seed `123456789`，单次请求超时 90 秒。没有做 prompt 对照实验或替换执行器。

完整首轮矩阵 **10/12 通过**，退出码 1，保存于 `full-surface-first/` 与 `full-surface-first.log`。两项模型失败均保留原始请求和回执，不把后续补跑合并成一次“12/12”。

| 场景 | 首轮结果与回执审查 |
| --- | --- |
| basic、structured、legacy_structured | 均通过；structured 的精确元数据字段、事实交付通过，解码说明有两次真实换行，原稿原样保留 |
| reread | 通过；正文读取两次，恢复快照 10 次 |
| stale_ref | 模型失败；寻找输入框与按钮的循环在找到输入框后立即 break，按钮 ref 始终为空，四次失败程序均未到真正 stale-ref 分支 |
| stale_edit / Hybrid | 模型失败；任务及首个模型请求均是正确 URL，模型把路径中的连字符抄成下划线；受控夹具对错误地址返回 404，尚未到版本校验 |
| upload | 通过；附件选择一次，选择后正文读取一次 |
| download | 通过；实际下载、持久化内容和哈希验证通过 |
| strict Research、Research sequence | 均通过；实际浏览器操作、提交和引用有效，sequence 满足后只开放提交入口 |
| source_failure、fixed_failure | 均通过预期负例；正式 HTTP 403 后 blocked，没有宣称读取成功 |

两项首次失败的请求均已确认含正确任务 URL，根因审查保存在 `first-failures-review.json`。错误 URL 的 404 来自受控夹具按约定拒绝不存在的页面，不是网络环境故障。两项均未启用新增说明事实或格式合同，失败点在浏览器程序内，没有据此修改生产合同。

为补齐未到达分支，两项各原样补跑一次，单独记录：

- `stale-ref-rerun/`：通过。真实旧 ref 被拒绝一次，之后重新 snapshot、点击、读取并完成。
- `hybrid-rerun/`：仍失败，保留退出码 1。本次模型先有 JavaScript 语法错误及错误编辑凭证，纠正后到达版本校验并产生一次真实冲突；冲突被正确拒绝。后续重新读取后，模型又把解包后的文件内容作为编辑凭证，最终 no_progress。最终文件保留注入的外部修改，不能称为恢复成功。

没有继续为取得通过而重复实机，也没有把文件内容自动当成编辑凭证。另运行既有固定回复生产回归 **7 项全部通过**，核验真实 Chromium 的 stale-ref 拒绝与恢复、正确外层读取凭证、真实版本冲突后的重新读取和编辑，以及矩阵分支统计，结果见 `remaining-surface-fixed-replies.log`。它证明正确程序能通过现有生产链，不能代替真实模型的恢复失败结果。

首轮、两次补跑和下面的说明合同专项，运行前后生产源码摘要均为：

`534be3a16e891780e512b7c151b8bd4c86be17a2f0b8539ce8d1a54adf89da49`

## 新增说明合同实机专项

`guarded-explanation-first/` 使用同一个真实模型、自主采集和生产 `run_case`，入口显式声明两个回执选值必须出现在说明中，并给 `/explanation` 加恰好两段的 schema。没有固定或改写模型回复。复现入口为证据目录中的 `live_explanation_contract.py`。

该次**声明的事实与格式合同通过**：当前回执值完整出现，说明解码后有真实空行而没有字面换行冒充分段；正文只读取一次，恢复快照 4 次，最终说明逐字保留。此次模型第一次 `done` 即满足新规则，没有实机完成拒绝；拒绝、原草稿反馈、只重交答案的机制由前述固定回复生产测试验证。

这份说明仍有模型错误：它多抄了任务要求不重复的元数据，并把说明中的 URL 抄成下划线路径。独立 JSON `page_url` 由原绑定填入，保持正确。该错误记录于 `guarded-explanation-first-review.json`，不把本次机械通过描述为整份说明的语义正确。当前新增规则只保证显式选中值出现和已声明格式满足，不能检查任意额外句子的真假，也不能保证模型每次能修正 ref 或编辑凭证错误。
