# 编辑工具改进与验证记录（2026-11-19）

## 修改范围

- 局部替换在统一换行视图中匹配，通过位置映射修改原始文本；保留未编辑区域的 CRLF/LF/CR、原始 BOM、末尾换行。替换区域内按行内容对齐继承换行，新增行采用局部换行。既有缩进辅助和行号前缀恢复共用同一映射与最终原子写入。
- 歧义失败最多展示三个候选的短上下文，保留原有行号和精确替换说明；超长行提示定点读取，整体仍受 1600 字符预算约束。找不到旧文本继续使用已有锚点上下文和差异反馈。
- 找不到旧文本，但长度至少 20 字符的新文本已存在于磁盘原文时，只提示“可能已应用”。不返回成功，不生成成功写入依据；不把尚未提交的批次中间结果作为依据。
- Python 片段支持两种有限恢复：旧、新片段都带有相同的末尾空格行边界，或都多带一个实际 EOF 不存在的换行。要求唯一匹配、行首边界、前后 Python 可解析；排除涉及多行字符串的情况。不会任意 `strip()`、修正内部缩进或归一化字符串标点。成功回执附带 `boundary_adjusted` 审计。
- 修复旧文本重叠出现时被误判为唯一匹配的问题，例如 `aba` 在 `ababa` 中出现两次；失败仍不写入。所有局部替换在最终写入前复用既有文件版本校验，批次失败不部分提交。
- 回归退出时发现 Windows 长路径清理失败：普通路径无法访问 264 字符的生产状态文件，已有 `filesystem_path` 可以访问。测试 RAM 盘清理复用该函数；新增实际长路径回归，并清除修改文件中的隐式动态类型。没有屏蔽警告或改变生产写入来适应测试。

整文件 `content` 更新保持既有行为，本轮的混合换行保留针对 `replacements`。仍使用既有读取合同、原子写入、版本记录和正式回执，没有新增编辑合同或执行运行时。

## 自动验证

最终相关生产与测试基础设施回归：**318 passed、2 skipped、18 subtests passed**，46.57 秒。两个跳过项都是 Windows 下不适用的 POSIX 权限位测试。最终退出没有先前的 RAM 盘清理警告。没有运行全量 pytest。

覆盖的测试文件：

```text
tests/test_edit_text_fidelity.py
tests/test_automatic_edit_assistance.py
tests/test_tool_runtime.py
tests/test_ambiguous_edit_guides_context_in_one_replacement.py
tests/test_conversation_edit_preview.py
tests/test_noab_hardening.py
tests/test_atomic_io.py
tests/test_versioned_content_edit.py
tests/test_edit_api.py
tests/test_edit_replay_preserves_trusted_workspace_audit.py
tests/test_edit_bump_failure_single_settlement_durable.py
tests/test_edit_rejects_legacy_aliases_before_execution.py
tests/test_edit_normalizer_has_no_parallel_contract.py
tests/test_edit_consumers_and_ab_truth.py
tests/test_native_edit_schema_matches_kernel_contract.py
tests/test_declared_work_continuation.py
tests/test_codemode_json_edit.py
tests/test_kernel_receipt_integrity.py
tests/test_pytest_temp_long_paths.py
tests/test_pytest_ramdisk.py
```

全树检查通过：

```powershell
python -m ruff check .
python -m mypy codey --python-version 3.11
python -m pyrefly check codey --python-version 3.11
python -m ty check codey --python-version 3.11
```

mypy 覆盖 684 个生产文件。pyrefly 报告 0 errors，同时仍有仓库既有的 35 suppressed、164 warnings not shown；本轮没有新增检查屏蔽。运行环境为 Windows、Python 3.12.8，类型目标为 3.11，不代表已在其他系统或解释器版本运行回归。

修改的 7 个 Python 文件另外通过限定到这些模块的 `disallow_any_explicit`、`disallow_any_expr`、`disallow_any_unimported` 检查。没有新增 `Any`、`cast`、`type: ignore` 或关闭检查。另一次小范围坐标枚举覆盖 CR/LF/CRLF 组合和全部替换边界，共 3601 例通过。

## 首次失败与修正历史

原始日志均保留于本机 `.e2e-artifacts/edit-assistance-20261119/`，未用重跑覆盖。关键记录：

| 记录 | 原始结果与处理 |
| --- | --- |
| `pytest-red.log` | 改动前新增回归 12 failed、11 passed；其中一个测试缺少生产要求的事先读取，修正测试，不放宽读取合同。 |
| `pytest-first-implementation.log` | 6 failed、49 passed；修复实现中计数函数被同名参数遮蔽的问题。首次局部 Ruff 发现复杂度问题，简化实现，没有增加屏蔽。 |
| `pytest-focused.log` / `pytest-expanded-first.log` | 分别 197、294 项通过，各 2 skipped。扩大回归首次暴露退出清理警告。 |
| `pytest-final.log` | 297 项通过、2 skipped；仍有清理警告，继续调查，没有忽略。 |
| `pytest-cleanup-red.log` | 实际长路径回归首次失败，证明问题在测试清理脚本。 |
| `pytest-cleanup-fixed.log` | 修复后相关 34 项通过，无清理警告。 |
| `pytest-after-cleanup.log` | 最终 318 项通过、2 skipped，无清理警告。 |
| `modified-type-precision-first.log` | 首次额外 Any 检查错误地覆盖了未修改依赖，出现 6971 个既有错误；改为仅对修改模块启用额外规则，保留依赖原严格配置。 |
| `modified-type-precision-after-cleanup.log` | 发现测试辅助脚本中 JSON 字典的隐式 Any；明确数据类型后，`modified-type-precision-after-cleanup-fixed.log` 显示修改的 7 个文件全部通过。 |

## 真实模型诊断

通过既有生产 `run_headless`、本机 Gemma4 服务及既有记录器运行隔离的小项目。每次保留任务原文、请求/响应、工具事件、原始文件字节和 durable SQLite 正式回执，没有替换模型输出或注入成功回执。

模型：`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`。

| 用例与目录 | 观察结果 |
| --- | --- |
| `live-ambiguous-first` | 读取 1 次，首次编辑因两个匹配失败，回执含两个函数的上下文；模型没有重读，随后提交的 `new_string` 漏掉函数定义，导致结果错误。按原始请求精确执行，属于模型输出错误。 |
| `live-ambiguous-second` | 加强任务中的完整替换说明后重跑，仍发生相同模型错误，保留失败结果，不继续重跑寻求通过。 |
| `live-boundary-first` | 原始请求确实带有末尾八空格。读取 1 次、编辑 1 次成功，正式回执含 `boundary_adjusted: 1`；最终字节只改变返回值，混合换行保持不变。 |

首轮歧义用例完成后，实验脚本导出回执时误用了不存在的 `ToolCall.arguments` 字段；修正为 `args`，从同一次原始数据库补导出 `receipts.json`，没有重跑来伪造首轮结果。原始导出异常在 `live-probe-first.log`，恢复摘要在 `live-ambiguous-first/summary-recovered.json`。

三个任务的后续收尾均请求了命令执行，被 headless 默认拒绝，最终 `stop_reason=stopped`。因此，边界恢复是**编辑步骤通过**，不能报告为完整任务通过；两次歧义用例的目标结果均失败。没有为模型漏掉上下文、额外执行命令或任务收尾问题修改生产合同。

这些是受控诊断，不是自然任务 A/B 试验。可确认边界恢复案例避免了一次编辑重试，但不能据此宣称总体节省比例，也不能认为失败上下文足以保证模型正确构造新文本。

## 剩余限制

- 新文本存在只是线索，无法证明先前在目标位置执行成功；成功仍以正式回执为依据。
- 不唯一、内部层级不符、涉及多行字符串或恢复后语法无效时拒绝自动边界恢复；其他语言不启用该恢复。
- 有界上下文可能无法展示区分位置所需的完整函数，超预算或超长行仍需定点读取。
- 版本校验沿用现有统一换行内容哈希，不新增原始字节版本合同；不声称解决只改变换行的并发变更，或最终检查与原子写入之间的全部竞争窗口。
- 精确替换仍执行模型明确提交的删除或代码变更，无法自动判断其是否符合用户意图。模型遗漏函数定义的实机失败说明这一边界仍然存在。
