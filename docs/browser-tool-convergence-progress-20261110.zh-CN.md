# 浏览器工具统一接线进度

目标：把浏览器操作能力接入 Codey 现有的 `ToolSpec → TurnSnapshot → ExecutionDelegate → kernel_facts` 链，保留一个任务内核、一个授权来源和一个 Research 证据账本。

## 批次记录

### 批次 1：生产合同和能力词汇

- 状态：已提交（`18ade5d8 feat(browser): register production browser contracts`）
- 已完成：为浏览器观察、交互和高副作用操作增加 capability；浏览器工具合同进入生产 `ToolSpec` 注册源。
- 默认行为：未授予浏览器 capability 时，普通任务工具面不变。
- 尚未完成：浏览器 executor、任务级 BrowserSession、`browser_read` 的证据投影、动态 surface、实机 gate。
- 验证：`20 passed`（ToolSpec、单一注册源、TurnSnapshot、能力词汇）；Ruff 和 mypy 均通过。
- KoboldCpp：`tools/local_model_diagnostic_probe.py --only p0 --json` 通过；生产 Chat `normal_chat` 通过（`.e2e-artifacts/browser-tool-convergence-20261110-batch1-chat`）。
- 备注：本批只增加正式合同和 capability，默认任务未获得浏览器 capability，因此没有改变普通任务工具面。

### 批次 2：统一浏览器资源、执行与正文回执

- 状态：已提交（`97f7e6cd feat(browser): route actions and captures through production kernel`）。
- `BrowserSession` 复用现有 `BrowserSearchProvider`/`BrowserWorker`；默认 connector → HTTP/browser 包装能够取到同一个浏览器资源，不启动另一套模型循环。
- `ResearchTools` 持有任务级浏览器资源；每轮 `ExecutionDelegate` 复用它，观察用的工具副本也共享它。浏览器对象不进入 durable state。
- 正式 browser executor 处理动作和 `browser_read`，不再依赖临时注册的 custom executor。
- `browser_read → SourceGateway.capture → 原正文渲染器 → ToolResult → kernel_facts`；沿用原有 acquisition 临时账本与提交后投影，执行器不直接修改正式账本。
- source-read 证据语义移到 `ToolSpec.evidence_route`；浏览器读取得到原来的 source_id、passage 和引用入口，动作不能夹带 Research 证据。
- 实验 `BrowserInteraction` 改为生产类的兼容导入；实验 capture adapter 转调正式 gateway，不保留另一份交互实现或接收逻辑。
- 修正迁移时发现的问题：导航可从空白会话启动；新标签使用原网络 guard；eval 输出向模型可见且使旧 ref 失效；登录读取失败沿用结构化 failure code。
- 修正原 CodeMode 构建边界：只在当前快照公开 CodeMode 时构建 executor，避免传入空快照。
- 验证：浏览器执行、原交互、SourceGateway、ToolSpec、TurnSnapshot 和 CodeMode replay 共 `51 passed`；相关 10 个生产文件 mypy、Ruff、kernel 类型精度 gate 通过。
- 扩展核对：receipt handle、入口与 snapshot 测试 `44 passed / 2 failed`。两项待查：同 URL 连续采集后的版本 handle 未失效；project 入口的明确查官方文档请求未授予 web.read。没有通过更改断言或跳过测试掩盖它们，后续批次核对根因。
- KoboldCpp：p0 与 Chat smoke 均通过；Chat 记录位于 `.e2e-artifacts/browser-tool-convergence-20261110-batch2-chat`。这不代表浏览器任务或完整 release gate 已验收。
- 尚未完成：入口浏览器授权、严格 Research 的操作工具面、CodeMode 浏览器目录、文件持久化回执、完整生产路径的 Research/混合任务实机。

### 批次 3：入口授权、冻结工具面与 CodeMode 收敛

- 状态：已提交（`718ab01e feat(browser): compose entry authorization and frozen CodeMode surface`）。
- 入口只接受显式 browser capability，拒绝项最后生效；`allow_web=False` 同时拒绝联网浏览器能力。不按任务关键词偷偷扩大权限。
- 复用 `build_turn_snapshot()` 组织工具面，没有新增并列 SurfacePlan。浏览器任务的叶子工具进入原 CodeMode 冻结目录，顶层保留聚合入口和控制工具；无浏览器授权的任务维持原展示逻辑。
- 严格 Research 在快照规划处组合已授权浏览器能力，`ANSWER_TOOLS` 仍然只负责答案合同。CodeMode 子调用回到原 `execute_turn()`，父回执只保存 `child_effect_ids`，不再次发布子证据。
- `browser_read` 沿用原 source-read 字符串结果；动作使用结构化 `value`，snapshot 提供声明过的 elements。Research 调查、acquisition budget 和引用范围通过注册元数据识别正文读取。
- Research iteration 与普通任务共享浏览器资源绑定，避免只有普通入口有 BrowserSession；runtime inventory 纳入生产模块。
- 修正实机发现的请求计量缺陷：JSON schema 的属性名可以是 `type`，递归扫描不能把该属性 schema 字典拿去做集合 membership。
- 回归：入口授权、浏览器实执行、CodeMode replay、冻结工具定义和实验边界共 `110 passed`；附件 schema 与 CodeMode host 等补充测试 `30 passed`。后者有既有 RAM disk 清理锁警告，退出码为 0。Ruff、相关生产文件 mypy、kernel 类型精度 gate 通过。
- 生产实机：新增 `tools/browser_production_live_gate.py`，仅注入模型连接和受控网页，不临时注册工具、不改 executor/答案合同。最新记录：`.e2e-artifacts/browser-tool-convergence-20261110-batch3-production-v3`。
- 实机结果：Chat 通过；Research 和 hybrid 未通过。三种模式均成功执行导航、观察、填写、点击、等待、正文捕获。Research 已收到 `[e1]` 原文和调查记录，但重复重做操作，最终 `no_progress`；hybrid 读到了实测数值，却只修改内存 JSON，没有调用写入，`done` 被原完成检查拒绝，随后 `no_progress`。不能把动作成功算成整任务成功。
- 早期实机失败也保留：动作结果缺少 `.text`、Research 资源未绑定、请求计量崩溃均已修正；测试网页缺少 UTF-8 声明属于 fixture 缺陷；模型误用 source-read 返回类型通过明确合同说明处理，未新建另一套返回类型。
- 批次 2 的入口联网失败已核实为旧测试合同漂移：现有入口明确使用显式能力授权，测试已对齐 `test_chat_web_entry_policy.py`，没有恢复按措辞授予 web.read。来源版本 handle 问题仍待修复。
- 验收边界：这些结果证明生产执行与证据接线可用，不证明当前模型在 Research/混合任务中稳定闭环，也不证明任意真实网页可靠。

### 批次 4：工具面迁移回归排查与当前检查点

- 状态：按用户要求先更新文档并提交检查点；Research 实机已恢复通过，**混合任务仍未稳定通过，不是完成验收的提交**。暂停继续扩展下载等能力，优先修复迁移回归。
- 对批次 3 失败的归因修正：不能仅归因于模型循环控制。迁移确实丢失了工具用途说明、已执行步骤和部分入口的一致性；这些是本轮找到并修改的具体问题。

#### 已确认的问题与修改

1. **工具说明丢失**：原 CodeMode declarations 只有参数与返回类型。叶子工具被隐藏后，原来的 description 也不再展示。现在从同一冻结 catalog 生成用途说明，保持 ToolSpec 为唯一语义来源；明确 JS 返回修改后的数据不会自动写入外部状态。
2. **Research 工作视图丢失操作进度**：原视图保留 browser_read 的正文和调查记录，却没有导航、填写、点击、等待这些已执行步骤。现在直接从已提交的 `session.executed` 和原回执投影有界 operation_receipts，普通工作视图与 Research 共用；不新增 checkpoint、账本或浏览器循环。该摘要不含填入的值、页面正文或 eval 内容，也不赋予引用资格。
3. **批处理包装与隐藏叶子不一致**：顶层还展示 read_files/parallel，但展开后的 read_file 等工具已被迁入嵌套目录，正常调用遭到当前 snapshot 拒绝。浏览器聚合面现在一并移除这些不可执行的外层包装，不放宽解析/执行授权绕过此问题。
4. **过度聚合项目写入**：批次 3 把项目 edit 也收入默认 CodeMode，混合任务出现“只返回内存 JSON 却宣称已保存”。当前将 edit 保留为明确的顶层动作，从该默认嵌套目录移除，避免双份展示；非浏览器任务和显式 model_tool_names 路径保持原选择方式。这个展示调整还未使混合任务实机稳定通过，见下文。
5. **显式禁止验证被无关词汇撤销**：任务中的“每一步检查工具结果”被旧的全句关键词启发式视为要求本地验证，覆盖了“不要运行测试或命令”，导致文件已写入却要求不存在的测试命令。现在显式执行否决优先；保留“don't skip tests”这种双重否定的含义，删除不再使用的正向关键词推断路径。没有降低项目变更必须有真实写入回执的完成检查。
6. **编译错误反馈不完整**：QuickJS 的 stack 不保证包含错误消息；旧反馈只给了调用栈。现在同时显示 SyntaxError 消息和有界、带行号的源码片段，明确没有子调用执行；不自动改写或重试代码。

#### 验证与实机记录

- 最新入口、真实文件写入/完成检查、catalog 说明与显式验证否决回归：`24 passed`。文件用例确认：成功返回内存 JSON 不改变文件且不能完成；原 edit 路径写入后，原完成检查才通过。
- 浏览器执行与合同相关回归一轮 `33 passed`；恢复授权、依赖方向、snapshot、CodeMode 写入及拒绝完成进度回归一轮 `81 passed`；此前扩展针对性回归一轮 `98 passed`。这些集合有重叠，不能相加作为独立测试数。
- CodeMode runtime、拒绝反馈和转义回归中新增了记录中的 `\r`/`eturn` 编译失败用例；该组其他 `76 passed`，同时暴露的新文件测试 fixture 缺少 effect_scope / revision store，已补齐并包含在上述最新 `24 passed` 中。未通过改生产 guard 规避 fixture 错误。
- Ruff、相关 6 个生产 Python 文件 mypy、kernel 类型精度 gate 和 JS 语法检查通过；未运行全量 pytest、覆盖率或安装打包测试。
- 实机始终复用 `tools/browser_production_live_gate.py` 的原任务提示，没有添加“必须使用某种写入代码”的新测试提示，也没有修改测试期望来接受内存数据。
- `.e2e-artifacts/browser-tool-convergence-20261110-batch4-contract-history`：Chat、Research 通过；hybrid 实际写回正确的 173 / QUARTZ_851，但被上述验证否决误判阻断。
- `.e2e-artifacts/browser-tool-convergence-20261110-batch4-fixed`：Chat、Research 再次通过，Research 一次导航后读取并提交 `[e1]`；hybrid 又出现只返回 JSON、没有写入的错误，故继续调整项目写入展示，不能声称已解决混合闭环。
- `.e2e-artifacts/browser-tool-convergence-20261110-batch4-write-surface`：hybrid 生成无效 JavaScript，重复相同编译失败，未完成。这暴露了上述错误反馈问题。
- **当前最近实机** `.e2e-artifacts/browser-tool-convergence-20261110-batch4-diagnostics`：hybrid 已完成完整浏览器读取和项目文件读取，但反复在 CodeMode 中调用不在嵌套目录的 tools.edit，没有转到已展示的顶层 edit，最后 no_progress；文件没有写回。
- 下一步需要解决当前顶层与嵌套路由的可用性问题，进一步验证展示策略及错误修复反馈；不能把这份检查点描述为“Research 和混合任务均已恢复”。

### 批次 5：Hybrid 统一嵌套写入路由

- 状态：已完成针对性实现和受控生产 gate 验证。
- 浏览器聚合面将真实 `edit` 放入冻结 CodeMode catalog，并从顶层移除；`tools.edit` 仍通过原 `execute_turn()` 执行，生成正常写入回执和完成证据。
- `run` 从浏览器聚合的 CodeMode 面单独排除，不因项目写权限自动出现；非浏览器任务继续保留原兼容行为。
- 仅返回修改后的内存 JSON 仍不会改变文件；只有实际 `edit` 子回执能满足 `project_changes_required`。
- 受控 KoboldCpp gate：Chat、Research、Hybrid 均通过；Hybrid 的 `result.json` 写回为 `{"amount":173,"code":"QUARTZ_851"}`。
- prompt 审计后将浏览器指导从七句压缩为三句：只保留顺序执行和 fresh ref、`browser_read` 字符串及引用资格、不确定动作先核对。CodeMode 的“返回对象不等于持久写入”和 operation receipts 仍保留，因为前者是写入真实性合同，后者是运行时事实恢复，不是重复提示。
- 精简 prompt 的针对性回归为 `39 passed`；独立 Hybrid 实机再次通过，实际写回同一组 `result.json` 值。浏览器工具 schema 和 CodeMode catalog 中的说明继续作为冻结合同生成，不额外复制到任务提示。

### 后续批次

1. 优先解决混合任务的顶层/嵌套路由与真实写入闭环；保持原任务提示、真实文件结果与完成检查，补实机验收。
2. 文件操作沿用现有存储、权限和回执：启用正式下载、持久化文件及摘要，落实上传文件范围约束。
3. 加强稳定 page_id 选择、元素语义新鲜度，继续利用原 progress/receipt 机制处理重复操作，不创建浏览器专用 agent loop。
4. 修复连续采集的来源版本问题。私有页面读取尚未具有独立的数据处理语义，不能声称已完成。

## 不变量

- 浏览器不创建第二个模型循环或第二个任务运行时。
- 浏览器动作只产生动作回执；只有 `browser_read` 产生可引用正文。
- UI、事件和恢复都从统一 `ToolResult`/receipt 投影，不双写浏览器事实。
- 修改的生产 Python 文件不得新增 `Any`；新增类型使用明确的 JSON、协议和结果类型。
