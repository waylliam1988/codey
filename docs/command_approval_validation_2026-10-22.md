# 命令审批改进验证（2026-10-22）

本轮修复 `run` 被命令白名单拦截后只显示“需要批准”、却没有真实审批记录的问题。
可申请授权的单条命令现在进入持久化审批工作流，保留原始工具名、参数和调用身份；
明确策略拒绝、当前模式缺少授权能力、Windows 不支持的 heredoc 分别返回可解释的结果。
混合工具批次不整体挂起，避免跳过或重放其他工具；需要审批的命令必须单独调用。

界面只根据真实审批记录显示批准按钮，同一命令位置更新审批和执行状态。
提供仅本次、相同会话精确命令、受限命令族的项目精确命令授权，以及 Settings 中的查看与撤销。
授权绑定项目、工作目录、原始命令、执行器和执行环境摘要；任意脚本不能保存为项目规则。
`run.output_tail_lines` 和命令输出末尾视图减少为截取输出而添加管道的需要，保留真实退出码。

## 已执行的验证

- 后端内核、完成、工具与跟踪回归组：1551 passed，95 subtests passed。
- 审批、恢复、HTTP 重启和原生工具续跑回归组：72 passed。
- 最新权限和公共事件回归组：33 passed，221 subtests passed。
- 补充审批后输出裁剪、非法参数和生命周期回归：44 passed；项目扫描与配置回归：19 passed。
- 前端完整测试：42 个文件，180 passed；TypeScript 类型检查和 Biome 格式检查通过。
- Ruff 通过；mypy 检查 501 个生产源文件通过；Pyrefly 0 errors；ty 通过。
  Pyrefly 仍有现存的 suppressed/warnings，不能视为零警告。
- 前端生产构建和发布清单一致性检查通过；`git diff --check` 通过。

这些是分组运行的结果，部分用例重叠，不相加宣称独立测试总数。

## 实机验证

`tests/test_shell_approval_browser.py` 使用实际发布前端、本机 HTTP 服务、Chromium 和真实子进程，
覆盖批准、拒绝、会话记住、项目记住四种情况：4 passed。
还覆盖批准响应丢失后的恢复、不重复执行、规则查看和撤销。
截图在忽略目录 `.e2e-artifacts/command-permissions/`，不提交运行现场。

`tests/test_release_shell_continuation_http.py` 使用本机确定性模型端点，
覆盖两种原生 API 协议、`run`/`shell`、成功/失败/拒绝，以及第二个任务复用会话授权后自动续跑。
这不是线上模型质量测试。

实测发现并修复了：自动授权在原任务上下文提交时触发过期 attempt token；
原始 `run` 结果恢复时仍按 `shell` 匹配；公共实时事件过滤掉项目和授权范围信息。

当前桌面服务运行的是已有不可变 release；新功能通过最新工作树启动的隔离服务验证。
MCP 已连接桌面服务并确认健康状态。随后由 MCP 派发 Codey 自行提交的任务，
实际提交 hash 和工作树状态应在任务完成后独立核对，不以任务入场回执代表提交成功。

MCP 实测发现：本仓库 `artifacts/external-control-live/state/..server.lock.lock`
被另一项既有服务占用，而工作区身份扫描尚未排除本地运行现场，导致 `run` 在执行前拒绝。
本仓库新增 `.codey/config.json`，使用现有 `scan.ignored_paths` 精确排除 `artifacts` 目录，
与本仓库已有 Git 忽略范围一致。没有跳过源文件扫描，也没有停止另一项服务；
实际完整 fingerprint 扫描随后成功。
