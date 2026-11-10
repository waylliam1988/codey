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

- 状态：已完成，准备提交。
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

### 后续批次

1. 将浏览器 capability 接入任务入口、surface 快照、文件操作和恢复；保持 `ANSWER_TOOLS` 为答案合同，复用现有 TurnSnapshot，不新增并列 SurfacePlan。
2. 收敛 CodeMode 的浏览器目录与展示，补生产路径的 Research/混合任务实机验收。
3. 核对扩展测试暴露的来源版本与入口联网问题，修正确定性根因并记录结果。

## 不变量

- 浏览器不创建第二个模型循环或第二个任务运行时。
- 浏览器动作只产生动作回执；只有 `browser_read` 产生可引用正文。
- UI、事件和恢复都从统一 `ToolResult`/receipt 投影，不双写浏览器事实。
- 修改的生产 Python 文件不得新增 `Any`；新增类型使用明确的 JSON、协议和结果类型。
