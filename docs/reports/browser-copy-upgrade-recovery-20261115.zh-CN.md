# 浏览器复制旧合同：升级后恢复已完成结果

审查对象：`93b6f460ab27cfdf241dcddcd5d52a1a93474d90`。确认存在升级恢复回归。

## 问题与修改

`drive_kernel()` 返回已提交的完成结果前，会通过 `KernelContext.restore_facts()` 恢复历史回执。历史模型回复也会进入 `restore_snapshot()`。原来的 `_restore_input_binding()` 要求冻结的浏览器复制合同与当前 `CopyTurn.spec()` 完全相同；因此，历史中只要有一轮使用旧合同，已完成的任务也会恢复失败。

本轮只修改一个生产模块：`codey/operations/provider_receipts.py`。恢复函数保留原冻结定义，并将合同是否匹配当前转换实现合入已有的 `implementation_compatible`。结构、权限、来源观察和合同文本校验继续执行。已有的两处执行兼容性检查继续拒绝尚需执行的不兼容旧调用；读取历史事实和返回已提交结果不再因合同升级被拒绝。没有恢复旧合同的执行实现，没有新增运行时或另一份合同。

## 回归证据

新增夹具 `tests/fixtures/browser_copy_legacy_binding.json` 来自 `1cbaab10` 的真实工具定义。测试仅保存该定义，运行时不依赖 Git，也不执行旧版源码。

`tests/test_browser_copy_upgrade_recovery.py` 覆盖：

- 恢复旧合同后，原工具定义和合同文本保持一致，执行兼容性为 false。
- 文本与 native 两种历史回复中，待执行的旧复制调用都被生产驱动拒绝，没有工具 effect。
- 通过真实 headless 入口、浏览器、文件写入和 SQLite 持久化，执行一轮带旧冻结定义的复制。在完成结果已提交、外层任务尚未返回时模拟中断，重新打开存储，再调用生产驱动两次。两次都返回原成功结果，恢复原说明和写入回执；没有新增模型请求、持久化事实或工具回执，完成后加入的外部文件修改保持不变。

原 `test_model_input_binding.py` 中只改变 schema、没有同步冻结合同文本的样例仍拒绝，断言调整为合同文本不一致。该文件原有的三处空容器补充明确的 JSON 元素类型，以消除 pyrefly 检出的隐式 `Any`。

修复前有效复现保留在 `reproduction-stored-result.log`：已完成结果为成功，重启恢复却得到失败结果。更早的 `reproduction.log`、`reproduction-completed-copy.log` 包含测试搭建问题，分别为缺少 `emit_jsonl`、错误地从外层终态尝试恢复；这些记录没有删除，也不作为生产缺陷的证据。

## 验证

所有本轮本地日志位于 `.e2e-artifacts/copy-upgrade-recovery-20261115/`。

| 检查 | 结果 |
| --- | --- |
| 相关生产回归，17 个测试文件 | 186 passed，239.57 秒，退出码 0 |
| 最后补充类型与成功断言后的两文件定向复核 | 23 passed，11.30 秒，退出码 0 |
| 全仓 `ruff check .` | 通过；首次仅新测试导入顺序失败，修正后通过 |
| `mypy codey` 加两个修改的测试模块，Python 3.11 | 690 文件通过；修改模块额外启用 `disallow_any_expr`、`disallow_any_explicit` |
| `pyrefly check codey --python-version 3.11` | 未通过：未修改文件中有 11 个既有错误 |
| `ty check codey --python-version 3.11` | 未通过：未修改的 `codemode/host.py` 中有 3 个既有错误 |
| 修改的三个 Python 文件，pyrefly、ty | 错误门通过；pyrefly 另有一个原有的多余 `bool()` 转换警告 |
| kernel 类型精度门 | 通过，受保护范围的 Any 计数为零 |

pyrefly 1.3.2 的 11 个生产诊断分为两组：`codemode/host.py` 的 3 处字典值类型推断不兼容，以及 `browser_completion.py`、`browser_followups.py`、`browser_task_handoff.py`、`kernel_context.py`、`task_execution.py` 的 8 处空容器类型推断。ty 0.0.87 的 3 个诊断也是前述 Host 字典类型问题。六个诊断来源文件的当前内容与 HEAD 经换行归一化后的 SHA-256 全部相同，记录于 `type-baseline-sources.json`。本轮没有修改这些文件、关闭检查或添加豁免。检查修改文件时首次发现的测试空容器问题已修复，首次和最终日志分别保留。

pytest 退出时有 RAM 盘临时目录清理的 WinError 3 警告；两个成功测试进程退出码均为 0。未运行全量 pytest，未创建或复制 worktree。

相关生产回归命令：

```powershell
python -X utf8 -u -m pytest tests/test_browser_copy_upgrade_recovery.py tests/test_model_input_binding.py tests/test_simplified_receipt_recovery.py tests/test_simplified_recovery_boundaries.py tests/test_browser_copy_production.py tests/test_codemode_json_edit.py tests/test_explanation_delivery_production.py tests/test_codemode_replay_authority.py tests/test_kernel_execution_snapshot_guards.py tests/test_browser_snapshot_recovery.py tests/test_restart_preserves_facts_and_pending_delivery.py tests/test_settled_delivery_recovery.py tests/test_terminal_delivery_preserves_completion_proof.py tests/durable/test_provider_snapshot.py tests/durable/test_provider_response_receipts.py tests/durable/test_provider_context_authority.py tests/durable/test_prepared_provider_request.py -q
```

## 实机矩阵

使用现有 `tools.browser_full_surface_live_gate`，实际模型为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，受控网页经生产入口和内核执行。

首次完整矩阵结果保留于 `full-surface-first/`，**11/12**，进程退出码 1。

| 场景 | 首次结果 |
| --- | --- |
| basic、structured、legacy_structured | 通过 |
| reread | 通过，9 次快照恢复 |
| stale_ref | 未通过：模型漏掉任务明确要求的旧 ref 尝试 |
| stale_edit（Hybrid） | 通过，真实版本冲突 1 次，完整重读后恢复写入 |
| upload、download | 通过 |
| research、research_sequence（strict Research） | 通过 |
| source_failure、fixed_failure | 通过，按预期阻塞 |

`stale_ref` 的原始任务明确要求“填入城市后，先尝试一次这个旧 ref 的 browser_click”。原始 `provider.jsonl` 第 2 次响应提交 `browser_fill`，第 3 次响应直接提交新快照中的 `g4e2`；最初按钮 ref 是 `g2e2`。`receipts.json` 确认点击使用 `g4e2` 且成功，没有任何 stale-ref 拒绝，`summary.json` 中 `failed_stale_refs=0`、`facts_match=true`。这次未覆盖所要求的失败路径，归因为模型漏执行步骤；没有观察到回执丢失、错误接受旧 ref 或本轮恢复回归。

单次复跑使用独立目录 `full-surface-stale-ref-rerun/`，**1/1 通过，退出码 0**；原始回复与回执确认实际触发了一次 stale-ref 拒绝，随后使用新 ref 完成操作，事实匹配。两次原始回复和相关回执的摘录保存在 `live-stale-ref-audit.json`。首次失败保留，不将复跑并入首次通过率，也不为该模型遗漏修改生产合同。

## 剩余边界

本轮恢复的是已有的历史事实和已提交完成结果。尚需执行的不兼容旧合同仍须重新发起请求；旧快照不因此获得当前实现的执行授权。全生产树 pyrefly、ty 的上述既有错误仍待各自范围内处理。实机矩阵验证受控网页下的新请求行为；升级后的已完成复制恢复由真实持久化的确定性生产回归覆盖。
