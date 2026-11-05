# Machine contract 失败复核（2026-11-05）

检查起点：`67c502a7`。CI 中 Windows 有 2 个失败，Linux 有 4 个失败；它们归为两个根因，不能统一解释为测试过时。

## 生产回归：native 首轮重复准备

`7f05b589` 为 native 工具结果续轮补上 coding context，但在 `KernelContext.freeze()` 中对所有非 acknowledgement 请求再次调用 `prepare_coding_context()`。完整首轮已经由 `prepare_kernel_turn()` 准备过，所以执行工具之前刷新次数由 1 变成了 2。Windows 本机在修改前复现了两个原失败节点。

修复是完整提示准备完成后显式通知冻结阶段，避免再次准备。工具结果续轮和重试仍按原路径准备当前上下文；冻结的工具声明、executor 绑定和持久请求来源不变。保留 `executions == [1]`，没有改成接受重复准备；增加普通 native / request-scoped native 参数，并断言首轮发送与续轮发送分别对应累计 1 / 2 次刷新。发送后替换工具注册表时，执行器仍必须是原 snapshot 中的版本。

## 测试隔离缺陷：模拟浏览器平台影响真实可执行文件查找

`532f65a9` 在测试 home 隔离之前加入了 `_preserve_bun_runtime()`，其中使用真实的 `shutil.which("bun")`。Playwright 缓存测试此前修改整个子进程的 `sys.platform` 来检查 Linux、macOS、Windows 缓存策略，导致 Linux 模拟 Windows 时也把 Python 3.12 的 `shutil.which()` 引入 Windows 分支。Linux 标准库的 `_winapi` 为 `None`，因而访问 `NeedCurrentDirectoryForExePath` 失败。Windows 自身具有 `_winapi`，不会出现这两个额外失败。

修复限于测试探针：缓存策略仍使用被模拟的平台，但真实的 `shutil.which()` 在宿主平台下执行，完成后恢复模拟值。没有把 Bun 查找替换成固定成功，没有删除缓存、重初始化、子进程环境或 home 隔离断言；另检查两次启动均经过宿主查找。生产代码和 `tests/conftest.py` 无需更改。

此前扩大组没有选择这两个失败文件，因此其绿色结果不能外推为 machine contract 全部通过。现在把既有的 native 编辑后上下文测试和 Bun home 隔离测试加入门禁清单，覆盖补充上下文与跨平台测试启动之间的联动。

## 验证

- 直接相关组：24 passed。
- 扩大组：22 个文件，162 passed（77.45 秒），无跳过。包含上述直接组，数量不相加。
- Linux 目标 mypy：581 个源码文件通过；Pyrefly：0 errors（35 suppressed / 80 warnings）；ty：Linux 和 Windows 目标均通过。
- 修改文件 Ruff、`git diff --check`、source size inventory 通过。

本机是 Windows / Python 3.12.8。缓存测试执行了各平台策略模拟，但没有 Linux 实机结果；最终 Linux CI 仍需推送后确认。本轮未运行全量 pytest 或完整 machine contract 门禁。

扩大组可在仓库根目录按以下 PowerShell 命令复现：

```powershell
$targets = @(
    'tests/test_workspace_native_turn_preparation_and_snapshot_ownership.py'
    'tests/test_native_coding_context_after_edits.py'
    'tests/test_pytest_home_isolation_preserves_playwright_cache.py'
    'tests/test_pytest_home_isolation_preserves_bun_runtime.py'
    'tests/test_pytest_home_isolation.py'
    'tests/test_coding_context.py'
    'tests/test_prepared_coding_context_is_immutable.py'
    'tests/test_native_coding_context_uses_native_instructions.py'
    'tests/test_kernel_prompt_rendering_has_no_side_effects.py'
    'tests/test_kernel_prompt_task_guidance_ownership.py'
    'tests/test_chat_workspace_tools_and_answer_completion.py'
    'tests/test_workspace_native_answers_and_tool_selection.py'
    'tests/test_kernel_protocol_repair_keeps_snapshot.py'
    'tests/test_native_final_answer_protocol_repair.py'
    'tests/test_local_length_continuation_kernel.py'
    'tests/test_native_cancel_and_budget_close_preserve_authorized_tool_declarations.py'
    'tests/test_verification_candidate_refresh_failure_blocks.py'
    'tests/test_verification_candidate_prompt.py'
    'tests/durable/test_prepared_provider_request.py'
    'tests/durable/test_provider_sink_generation_boundary.py'
    'tests/durable/test_provider_response_receipts.py'
    'tests/test_recovery_restores_research_ledger.py'
)
python -m pytest -q --tb=short @targets
```
