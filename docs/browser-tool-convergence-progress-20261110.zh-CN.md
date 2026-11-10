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

- 状态：已完成，随本批提交。
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

### 后续批次

1. 文件操作沿用现有存储、权限和回执：启用正式下载、持久化文件及摘要，落实上传文件范围约束。
2. 加强稳定 page_id 选择、元素语义新鲜度，继续利用原 progress/receipt 机制处理重复操作，不创建浏览器专用 agent loop。
3. 修复连续采集的来源版本问题；继续记录 Research/混合任务闭环实机失败。私有页面读取尚未具有独立的数据处理语义，不能声称已完成。

## 不变量

- 浏览器不创建第二个模型循环或第二个任务运行时。
- 浏览器动作只产生动作回执；只有 `browser_read` 产生可引用正文。
- UI、事件和恢复都从统一 `ToolResult`/receipt 投影，不双写浏览器事实。
- 修改的生产 Python 文件不得新增 `Any`；新增类型使用明确的 JSON、协议和结果类型。
