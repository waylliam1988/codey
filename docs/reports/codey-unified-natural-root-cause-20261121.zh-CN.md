# 本地自然任务失败：原始证据、机制核查与修复

> 本文保留最早四次自然失败的证据与归因。后续自动复制、worker、视觉和人工协助的修复及新实测见[后续实施与验证](codey-unified-agent-browser-worker-and-validation-20261121.zh-CN.md)；后续通过不抵消本页的失败。

日期：2026-11-21，Asia/Taipei。分析基线：`06640d73`。

## 结论

**不能把前两次失败只归为模型错误。** 实际请求中存在确定性的生产提示缺陷：组合交付混入旧研究合同；字段仍未取得时，交接只突出尚无成功回执的 `run`；完整页面缺少目标字段时，反馈仍要求重读。模型同时提交了不允许的命令和有误的 shell 引号，没有完成用户要求的查询。

原有“减少抄写”机制没有整体被删除。实际冻结的发送合同里，语义控件选择和固定浏览器 follow-up 已启用；字段自动复制因来源缺少实际值而正确地没有启用。**问题包括组合提示迁移遗漏和证据状态展示不完整，不能用“函数还在”代替端到端验证。**

本轮修复了上述三处提示问题。两次新增自然复测仍失败，四次都不计为闭环通过。修复使机械状态与提示一致，不代表已经解决本地模型的页面操作和上下文压力问题。

逐请求大小、原始回复、真正发送快照、回执重放及 SHA256 见 [诊断数据](data/codey-unified-natural-root-cause-20261121.json)。日志摘要和修改文件摘要见 [本轮验证清单](data/codey-unified-natural-root-cause-validation-20261121.json)。

## 证据的读取边界

保留原目录 `.e2e-artifacts/unified-natural-first/`、`unified-natural-second/`，新目录为 `unified-natural-guidance-third/`、`unified-natural-feedback-fourth/`，没有覆盖重跑。

不能从 `snapshots.jsonl` 的基础快照推断模型最终没有浏览器入口：实验的 `restored_snapshot()` 在 `build_turn_snapshot()` 后记录，之后生产还会运行 `task_handoff()`。核查以 durable `provider_request.payload.snapshot` 和 HTTP 请求中的最终合同为准。前两次每份真正发送合同都启用了 `browser_followups`，绑定了 4 个观察到的控件，并提供了 `browser_fill`、`browser_click`、`browser_read` 等入口。

前两次是在上一批开发途中执行的，不是最终提交后重新运行的验收。上一批末期的动作前提修复不能反向算作这两次已通过。本轮则用原 durable 回执在当前代码上重放，确认“字段缺失却只突出 run”的问题确实还在，随后增加回归并修复。

## 四次自然执行

四次任务、模型、seed=123456789、temperature=0、14 轮上限相同。任务要求：查询福州记录，先报告，再修改 `result.json`，运行本地检查并交付。

| 执行 | 模型实际动作及停止原因 | 用户目标 |
| --- | --- | --- |
| 首次 | `browser_read` → `run` 被 `command_not_allowed` 拒绝 → 第三次生成前估算 24729 > 输入预算 24576 | 未查询、未报告、未编辑 |
| 第二次 | `browser_read` → `run` 被拒 → 改用 `shell`，因 `shell_syntax_unsupported` 被拒 → 回到同一 `run` → `no_progress` | 未查询、未报告、未编辑 |
| 第三次，修复状态展示和旧研究提示后 | 连续三次 `browser_read` 未加载页面 → `no_progress`，没有提交上述命令 | 未查询、未报告、未编辑 |
| 第四次，再修复完整来源缺字段反馈后 | 读取 → 点击 Locality 输入框 → 使用旧 ref 被拒 → 观察 → 点击当前控件 → 再读取/观察 → 第八次生成前估算 25564 > 24576 | 未填写城市、未取得字段、未报告、未编辑 |

第二次并非每一步完全原样重复：它确实尝试切换到 `shell`，但保留了错误的引号，之后又退回原来的 `run`。第四次的原子点击成功也不等于完成记录查询。四次最终文件都仍为 `amount=0`、`code="unknown"`。

前两次的命令只是打印当前占位值，既不写文件，也没有断言网页字段与文件一致，即使允许执行，也不能满足用户目标。`run` 拒绝任意 `python -c` 符合现有验证命令策略；`shell` 在语法检查阶段即失败，不能把它说成已经申请审批。本轮没有扩大命令白名单、自动改写错误命令或伪造审批。

还逐条比较了原始回复中 JSON 解码后的 `command` 和 durable 工具回执参数：首次 1 条、第二次 3 条均完全一致。错误的内外引号不是客户端规范化或工具 lowering 引入的；第二次省略的 `path` 则按既有规则补为 `.`。

## 确定性的生产根因

### 1. 新旧研究交付合同同时出现

前两次原始请求一处要求用 `[eN]` 和 `submit_research_answer` 提交完整报告，另一处却要求先调用 `research_plan`、只采用 `[fN]`、向 `done.report_sections.coverage` 提交选项。当时 `research_plan` 没有进入可见工具目录。

根因是 `task_guidance_for_policy()` 仍按 `task_kind != "research"` 决定是否注入旧规则；上一批已允许 hybrid/project 持有统一报告义务，这个旧判断没有随迁移更新。旧的“免重写、由保存的 finding 编译报告”机制适用于旧 finding 合同，混入 whole-answer 合同后反而成为冲突指令。

本轮把判断收回 `answer_contract` 的同一事实来源：`whole_research_policy()`，原 `whole_research(session)` 也委托给它。组合任务与纯研究按同一个冻结交付义务选择规则；旧的 strict finding 流程仍保留原机制，原提示摘要回归也继续通过。

### 2. 字段缺失被另一个缺执行项遮住

原回执正文只有“尚未加载记录。请填写 Locality 并点击 Load record”，不含净重和校验码。机械重放两个文件字段的 observation 都是 `observation_selector_unavailable`，`run` 则是 `tool_execution_missing`。

字段并没有被校验器算作成功。缺陷在展示层：`browser_task_handoff()` 的分支先显示 missing execution，只有没有这类项时才展开其他未完成证据；`working_context()` 也只列缺执行/缺父回执。于是实际最新提示变成：

```text
Next step from committed receipts (reuse completed work):
Outstanding declared calls: run.
```

这不是可靠的下一步：取证、报告、编辑都还没完成。统一交接把这个原有遗漏带入 strict 组合任务，不能只责怪模型选择了提示中唯一突出的工具。

本轮分别展示缺失观察和缺失执行，保留来源选择器诊断，明确先取得字段再复制、编辑后再验证；义务清单不代替用户规定的执行顺序。没有增加第二套进度状态或让观察失败绕过完成门。

### 3. “读完但没字段”仍被提示为“再读”

第三次把第 2 项修好后，模型从重复 `run` 变成重复读同一未加载视图。原选择器失败反馈要求“读取完整当前文档”，没有告诉模型：完整读已经成功，但目标字段尚未出现在该视图中。

`ToolSuccessRequirement` 原来只给 report 字段提供“缺字段应继续查询，重读未变化视图不会加载数据”的诊断，文件字段走通用 selector 失败分支。本轮复用同一缺字段反馈，明确保留字面选择器，要求查看当前查询/详情控件；已派发点击仍按原 wait/read 恢复，不能重复点击。选择器、值类型、来源回执和审批合同都没有放宽。

这些事实证明生产提示有缺陷，也说明修复后模型行为发生了变化；多处提示同时变化，不能据此证明某一条提示是模型选择特定命令的唯一原因。

## 上下文失败的准确归因

这几次实验使用 `ExperimentProvider`，直接配置总窗口 32768、输出保留 8192、保留近期 12000，得到输入限额 **24576**。请求计数方式是 `estimated`，不等于生产 Local 工厂接入的 Kobold 完整模板 tokenizer。不能把输入限额说成模型总容量，也不能把本地预检拒绝说成服务端实际爆上下文。

首次第三次生成未发出 HTTP 请求。两个已发请求的服务端 `prompt_tokens` 分别为 15904、16849，客户端估算为 23279、24123；计数口径差异需要单独保留。第二次首个 system 文本由 47223 降到 40314 字符，减少 6909 字符；`response_format` 仍有 26537 字符，指令、外层合同、CodeMode 签名与逐轮状态也占用预算。去重有实际作用，但第四次多轮恢复仍触顶，说明它没有解决整体预算分配。

`_admit_request()` 已按完整请求预检并尝试压缩。报错说明没有获得可用的进一步压缩结果，不能说“没有压缩机制”，也不能直接断言真实模型一定装不下。现有近期保护、固定合同和结构 schema 的成本不能仅靠删一段历史解决。把旧指令去掉和保留正确计数是本轮所做的；适配真实 Local tokenizer、进一步减少重复展示和按预算生成紧凑发现视图仍未完成。不能通过丢弃授权工具、改大配置数字或删除失败回执冒充修复。

## 减少抄写和恢复机制是否用上

| 机制 | 前两次真实状态 | 判断 |
| --- | --- | --- |
| 来源访问后自动快照 | 自动执行 `open_url` + `browser_snapshot`，保留来源 passage 与控件 | 已实际执行；取得的是未加载页面 |
| 语义控件选择 | 实际发送快照绑定 4 个控件，fill/click schema 提供完整结构选择 | 已提供，模型未正确使用；不能根据基础快照误判为丢失 |
| 固定浏览器 lowering | 模型 `browser_read {}` 被降为既有 CodeMode 程序并生成父/子回执 | 已实际执行 |
| 填写后自动观察；结果点击后自动读取 | 合同中已提供；前两次没有 fill/click，第四次使用的是 `purpose=action` | 具备条件但未走到；不能计作实际验证成功 |
| 原文填写绑定 | `browser_fill_text=null` | 合理未触发；该机制针对显式多行原文，不是自动从“查询福州”推导任意表单填写 |
| 字段自动复制、读版本引用和类型保留 | `edit` 提供 `declared_fields`，但 `input_binding=null` | 来源值未取得，正确地没有提供 path-only 固定复制；成功来源后的 path-only 合同另有机械回归 |
| 自动网页表格/JSON copy 模式 | `browser_copy=null` | 本任务不匹配该专门模式，且处于 strict 研究合同；不是任意网页任务通用兜底 |
| 已完成子动作不重放、return-only repair | 未出现符合条件的 CodeMode 返回值组装错误 | 不适用于直接 `run` 白名单拒绝和 shell 语法错误；既有 repair 回归通过 |
| 最终字段/报告自动组装和冻结 | 没有成功提交报告或完成编辑 | 未到达，不能说本次自然任务已覆盖；旧 finding 自动编译提示混入新合同已修复 |

上一批没有改动 `declared_field_input.py`、`browser_references.py`、`codemode/replay.py`、`codemode/shape_repair.py`。`browser_followups.py` 的相关变化是解除 scope/任务标签隔离并增加复用入口，不是删除 fill/read 固定程序。

另需区分**实验固定答案/采集**与生产机制。`ExperimentProvider` 的一些旧实验可注入完整采集 JS；这两次自然实验没有 `scripted_acquisition`，没有替模型填写城市和点击按钮。脚本化调用链成功不能证明模型自然选择成功，也不能把自然实验没注入固定采集说成生产机制被删除。

## 实验与尚存限制

自然语言任务本身没有点名 `run`，但实验仍额外注入两个 browser→文件字段绑定和一个裸 `tool_succeeded:run`。临时项目初始只有 `result.json`，没有现成业务检查脚本。因此这不是“完全无工具义务提示”的实验，裸 `run` 又增加了自行构造合法检查命令的负担。后续应把此配置与无额外工具声明的任务分开验收，继续检查实际比较了哪些字段，不能只看某个命令 exit=0。

还有一个未由这些自然运行覆盖的组合边界：来源齐备后，`declared_field_input.offer()` 会把外层入口暂时收窄到固定复制和询问。它目前没有把“先提交报告”纳入 `before_edit`。不能用现有来源齐备的复制测试，声称已经验证了“报告→固定复制→验证”的顺序；这需要继续做明确的组合回归，不能仅靠模型自觉或本轮提示修复宣告完成。

本轮没有完成原函数级方案全部批次，也未实施 async worker、完整 frame/ref 合同和截图入模。四次自然失败均保留。后续优先验证组合顺序和预算，再做自然闭环验收，不能回退授权统一来获得较短的提示。

## 本轮验证

验证只运行相关选择，没有运行全量 pytest。首败、修复重跑分别留档：机制核查首跑 6 failed / 76 passed；新状态展示回归首跑 2 failed；旧研究合同回归首跑 4 failed / 1 passed（其中 research 的正向输出断言也需修正）；缺字段反馈回归首跑 2 failed。修复和测试迁移不改变生产授权、字段合同和命令限制。

旧测试迁移包括：用授权和显式否决代替任务标签隔离；在 durable owner 清理前通过独立 CDP 观察点击次数；更新缺字段错误的文案匹配。修改的提示所有权测试同时迁移到正式 `ProviderTransport` 和生产 durable driver，并补齐文件中的类型，移除未标注替身和 `json.loads` 动态值。

最终结果如下，命令和日志摘要见本轮验证清单：

| 范围 | 结果 |
| --- | --- |
| 浏览器机制、语义 refs、follow-up、return-only repair、交接和执行回执批次 | 首跑 6 failed / 76 passed；迁移旧测试后 195 passed |
| 研究交付、组合能力、提示所有权批次 | 2 failed / 122 passed；2 项仍期待研究标签隐藏已授权写工具，已迁移到显式否决场景 |
| 字段/提示回归第二批 | 2 failed / 140 passed；2 项是缺字段错误文案匹配，修正后该函数 4 项通过 |
| 最终冻结的字段、提示、声明执行和 lineage 回归 | **176 passed**，包含上述错误匹配和提示所有权修复；未运行仓库全量 pytest |
| 全仓库 Ruff | 通过 |
| 生产树 mypy / pyrefly / ty，目标 Python 3.11 | 均通过；mypy 714 文件。Pyrefly 另有既存 suppression 和 warning，不是零 warning 声明 |
| 本轮 10 个修改 Python 文件 | Pyrefly 0 errors，ty 通过；mypy 修改文件自身 0 诊断，但完整调用仍报告 2 个未修改实验工具的 connector 类型错误。AST 未发现 Any、cast、忽略规则或未标注函数 |
| 扩大到 `codey tests tools` 的源代码全树 | mypy **18521 errors / 1276 files**，检查 2521 文件；pyrefly **21719 errors**；ty **5135 diagnostics**。未将此范围宣称通过，未扩大为整仓库类型债清理 |

直接以仓库 `.` 扫描时，mypy 先遇到非 Python 包目录 `disk-cleanup`，pyrefly 遇到旧 artifacts 的悬空符号链接；保留这些首跑，再显式传入三个源代码目录完成扩大检查，没有修改用户目录、链接或检查配置。所有命令均在现有工作目录执行，未创建 worktree。

自然实机、脚本化回归、修改文件诊断和仓库全树诊断是不同结论。不能把其中一个绿色结果抵消另一个失败，也没有关闭检查或增加忽略规则。
