# Shell 审批续跑竞态与相关回归检查

日期：2026-11-07。

## 调度通知丢失：生产代码缺陷

审批结算会将原任务重新置为 `PENDING`，并向调度器发送通知。
调度器每轮处理任务前已经消费了本轮通知；此后新出现的待调度记录属于后续提交。
原实现却会在读取到非待调度状态、领取冲突、取消或其他跳过分支中再次删除待调度记录。

可复现的顺序如下：

1. 调度器取出任务并消费当前通知。
2. HTTP 工作线程领取任务，随后等待审批。
3. 调度器读取等待状态，或领取失败。
4. 审批完成，将原任务重新入队并通知调度器。
5. 调度器根据第 3 步的旧观察跳过任务，同时清除了第 4 步的新通知。
6. 调度器退出空闲线程，任务留在持久化待运行状态，等待下一次无关通知。

修复移除了跳过分支对待调度集合的二次清理，保留入口处对本轮通知的单次消费。
持久化任务状态、attempt token 和领取冲突检查继续决定任务是否可以执行。
新通知可能使调度器多核对一次已结束或仍在等待的任务，但不会授权重复执行。

`tests/durable/test_scheduler_wakeup_races.py` 增加两种确定性交错：
旧等待状态和领取冲突。两种用例在修复前均失败，修复后均通过，且要求续跑恰好调度一次。
原有空闲退出、队列扫描和成功派发后的交错用例继续保留。
该文件已加入 `tools.machine_contract_gate` 的必跑集合。

用户提供的 Zen HTTP 用例在修复前单独运行、连续重复 16 次和运行完整 Shell 测试组时均通过。
因此，此处确认的是已经复现的通知丢失缺陷；仅凭原 CI 日志无法证明当时命中的就是此窗口。
本次没有延长超时或降低原 HTTP 用例的完成条件。

## 扩大测试发现的问题分类

| 问题 | 分类与证据 | 修复 |
| --- | --- | --- |
| 源码运行时无法发布 | 生产发布元数据过时；`codey/runtime.json` 仍列出旧前端资源，并缺少已提交的 Python 模块 | 使用 `python -m tools.publish_runtime` 重新生成清单，保留严格的完整性校验 |
| 进程恢复测试未到达故障注入点 | 测试工具落后于生产接口；工具读取已删除的 `KernelEffectSink._request_context`，实际终止信息为 `AttributeError` | 改为读取公开的、带类型的 `request_context`，并要求原始上下文存在 |
| 未知请求与损坏回执测试提前失败 | 测试替身不符合当前接口；无约束 `Mock` 自动生成了无效的 `window_ref` 和真值能力属性，内核先拒绝配置 | 显式设置 `window_ref=None`、`native_tools=False`，继续断言未知请求不重发、损坏回执不执行 |
| 来源收据测试访问字典属性失败 | 测试落后于内部类型接口；`_proof_evidence_refs` 接收 `CompletionContext`，测试仍直接传字典 | 在调用私有辅助函数前构造或解码成 `CompletionContext`，保留未读来源、完整 URL 和正文版本断言 |
| Research trace 精确比较失败 | 测试预期未包含生产新增的审计诊断字段 | 补充完整的成功审计阶段及状态断言，保留原有 trace、敏感信息和最终事件断言 |

恢复测试工具仍在真实持久化结算后通过 `os._exit(75)` 注入进程死亡，随后验证原结果恢复、原调用身份、授权和仅执行一次。
没有为旧私有字段添加生产兼容分支，也没有放宽协议窗口验证、回执摘要或运行时清单验证。

## 验证范围

未运行全量 pytest。检查范围包括 durable 目录、CI 的完整 Shell 测试组和运行时发布相关定向用例。机器契约门禁中的类型检查器反例测试已删除：它们会为每个错误样例分别启动 mypy、Pyrefly 和 ty，重复验证正常生产树类型检查已经覆盖的边界，并造成不必要的本机资源消耗。机器门禁仍保留轻量的 AST 类型精度检查；完整门禁适合在 CI 中运行。

```text
python -m pytest -q tests/durable -o faulthandler_timeout=120
python -m pytest -q tests/test_shell_contract.py tests/test_shell_interpreters.py tests/test_shell_analysis.py tests/test_shell_analysis_authorization.py tests/test_command_permissions.py tests/test_shell_approval_epoch.py tests/test_release_shell_continuation_http.py
python -m pytest -q tests/test_runtime_publication.py
python -m ruff check .
python -m mypy codey
pyrefly check codey
ty check codey
python -m tools.publish_frontend --check
git diff --check
```

本次修改未新增 `Any`、类型忽略或类型检查豁免。

已完成的结果：durable 目录 **734 通过、5 跳过**；完整 Shell 选择 **149 通过、11 跳过**。
这些跳过按原测试的环境条件保留；机器契约门禁仍要求其固定集合零跳过。
Ruff、mypy（624 个生产源码文件）、Pyrefly、ty 和前端发布检查均通过。
