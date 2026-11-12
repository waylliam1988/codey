# 2026-11-12 CI 失败根因与修复

在 Windows / 默认 PowerShell 7 环境复现了类型门禁、发布前端一致性检查及原始六个 Shell 测试失败。

| 失败 | 分类与根因 | 修复 |
| --- | --- | --- |
| `kernel_type_precision_gate` | 生产类型契约缺口：`_browser_call` 使用 `Callable[..., T]`，不能检查回调参数 | 使用 `ParamSpec` 保留调用签名，在浏览器工作线程内执行闭包；不放宽门禁 |
| `publish_frontend --check` | 随包发布产物过期：重新构建后仍精确重现缺失/多余的 46 个 JS 文件、HTML 与源码指纹不符 | 使用正式构建与发布命令同步 `codey/web`，保留一致性检查 |
| `test_changed_binary_is_rejected_before_any_spawn` | 测试边界错误：替换进程启动函数后才创建票据，捕获了准入阶段的可信 PowerShell AST 解析进程 | 先完成票据与分析，再观察执行阶段；生产指纹校验无需修改 |
| `test_opaque_request_requires_once_but_can_still_be_explicitly_approved` | 测试夹具方言错误：测试选择 CMD，夹具却重新解析默认 shell；`echo ^literal` 在 PowerShell 下是可复用的字面命令 | 夹具显式接收并持久化指定 `ShellSpec`，按各方言构造不透明命令 |
| 四个 HTTP `shell_failure` 用例 | 门禁探针假设错误：PowerShell 的 shell 退出码与 Python 子进程退出码不同；没有显式 `exit` 时原生失败被报告为 1 | 仅 PowerShell 的失败探针增加 `; exit $LASTEXITCODE`，仍严格验证退出码 7 |

生产 `ShellSpec.invocation`、执行器、授权策略和审批恢复行为均未调整。原有契约已经明确：PowerShell 若需传递原生程序的退出码，应立即保存 `$LASTEXITCODE` 并显式退出。

二进制变化检查、一次授权检查和真实发布探针均扩展到 CMD、PowerShell 7、Windows PowerShell、Bash、Zsh、Sh 参数矩阵；未安装的解释器沿用现有跳过规则。HTTP 测试继续通过真实审批、进程执行、持久化任务和两种 API 协议验证原始结果回传。
