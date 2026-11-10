# 浏览器工具统一接线进度

目标：把浏览器操作能力接入 Codey 现有的 `ToolSpec → TurnSnapshot → ExecutionDelegate → kernel_facts` 链，保留一个任务内核、一个授权来源和一个 Research 证据账本。

## 批次记录

### 批次 1：生产合同和能力词汇

- 状态：进行中
- 已完成：为浏览器观察、交互和高副作用操作增加 capability；浏览器工具合同进入生产 `ToolSpec` 注册源。
- 默认行为：未授予浏览器 capability 时，普通任务工具面不变。
- 尚未完成：浏览器 executor、任务级 BrowserSession、`browser_read` 的证据投影、动态 surface、实机 gate。
- 验证：`20 passed`（ToolSpec、单一注册源、TurnSnapshot、能力词汇）；Ruff 和 mypy 均通过。
- KoboldCpp：`tools/local_model_diagnostic_probe.py --only p0 --json` 通过；生产 Chat `normal_chat` 通过（`.e2e-artifacts/browser-tool-convergence-20261110-batch1-chat`）。
- 备注：本批只增加正式合同和 capability，默认任务未获得浏览器 capability，因此没有改变普通任务工具面。

### 后续批次

1. 统一 BrowserSession 与现有 BrowserWorker，并由 `ExecutionDelegate` 路由浏览器工具。
2. 将 `browser_read` 接入 SourceGateway、ResearchLedger 和 kernel facts，移除实验临时 ledger。
3. 将浏览器 capability 接入任务入口、surface 快照、审批和恢复；保持 `ANSWER_TOOLS` 为答案合同。
4. 把 live gate 改为生产路径验收，运行相关离线测试、Ruff、类型检查和 KoboldCpp 混合任务实机。

## 不变量

- 浏览器不创建第二个模型循环或第二个任务运行时。
- 浏览器动作只产生动作回执；只有 `browser_read` 产生可引用正文。
- UI、事件和恢复都从统一 `ToolResult`/receipt 投影，不双写浏览器事实。
- 修改的生产 Python 文件不得新增 `Any`；新增类型使用明确的 JSON、协议和结果类型。
