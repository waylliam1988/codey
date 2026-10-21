# Ghost 实验进度记录：Research State Contract

- 实验名称：Research parity state contract
- 状态：`EXPERIMENT_COMPLETE`
- 负责人：Codex
- 开始日期：2026-10-21
- 最后更新：2026-10-21
- 关联 Roadmap 验收门：0.6.1 Context Epoch admission；0.6.2 控制边界
- 实验 commit：`41167a7a`（生产契约）；`23e4a4b6`（parity/test fixture）；
  `8abb6a7f`（settlement frame fixture）；`50cc333d`（durable cold-start lock ordering）；
  `ea33efbe`（concurrent cold-start regression）
- 生产合并结论：`SHADOW_ONLY`

## 本次目标

确认 research 的真实生产依赖包含 `TaskRunDeps.state`，并验证缺少 state
不会由生产代码隐式打开 Ghost admission。假设是：冷启动和测试 fixture 都能
显式提供 state；Ghost 控制缺失时应 fail closed；旧 parity 行为由 fixture 中
明确开启的控制表达，而不是由生产 fallback 维持。

## 实际改动

- `run_research_iteration` 直接读取必填的 `deps.state`，移除缺失 state 的
  `getattr(..., None)` 兼容路径。
- `ghost_control_enabled` 在没有 `ghost_inbox` 时返回 `False`，不隐式放行
  `admit_hints`、检索或其他 Ghost 控制。
- research parity probe、research iteration adapter、Deep Research Core
  fixture 和直接调用生产迭代器的测试都显式提供 state，并把四个 Ghost 控制
  设为开启以复现既有 parity 基线。
- 修复 Context Epoch 过期时间的类型收窄和 Ghost control scope 空字典类型，
  使全树类型检查可复现通过。
- 全量 pytest 暴露的四个 settlement 失败均来自测试构造的 `frame` 缺少生产
  `RunFrame.project_text`，由 `8abb6a7f` 补齐 fixture；没有修改 Ghost 生产逻辑。
- 全量 pytest 暴露一个真实的 durable 并发冷启动竞态：schema 初始化曾在写锁
  之前读取 `user_version`。`50cc333d` 将版本读取移入 `BEGIN IMMEDIATE` 后，
  `ea33efbe` 增加并发冷启动回归，避免 reader 观察到未提交的临时版本。

## 验收命令与结果

```powershell
python -m pytest tests/test_kernel_legacy_parity.py -q
```

结果：`694 passed`。

```powershell
$files = Get-ChildItem tests -Filter 'test_ghost*.py' | ForEach-Object { $_.FullName }
python -m pytest @files tests/test_learning_switch_fail_closed.py tests/test_task_entry_cutover.py tests/test_task_guidance_reaches_production_entries.py tests/test_research_iteration_topic_continuity.py -q
```

结果：`433 passed, 136 subtests passed`。

```powershell
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
```

结果：mypy `Success: no issues found in 480 source files`；Pyrefly `0 errors`；
ty `All checks passed`。

```powershell
python -m pytest tests/test_ghost_vectors.py tests/test_ghost_projection_exception_boundaries.py -q
git diff --check
```

结果：`9 passed`；diff check 通过。

```powershell
python -m pytest -q
```

结果：`9177 passed, 59 skipped, 1518 subtests passed in 2659.24s`。

```powershell
python -m pytest tests/durable/test_database_cursor.py::test_concurrent_cold_start_serializes_schema_initialization tests/stress/test_multiprocess_concurrency.py -q
```

结果：`2 passed`；连续独立运行 multiprocess stress 3 次均通过。

## 实验配置

- 模型 / suite / endpoint：无 KoboldCpp；shared kernel deterministic fake provider；
  parity legacy/current probe
- tasks / cells / reps：kernel parity 全部 research/coding/protocol cells；Ghost
  state contract、admission、vector retrieval、projection exception fixtures；每项 1 次
  deterministic replay
- on/off 对照：显式 state + 四控制开启 vs 缺失 Ghost inbox fail closed；legacy/current
  parity 对照

## 结果与限制

- 生产调用链和 parity fixture 现在使用同一依赖形状；无 state 不再改变生产行为。
- 缺失 `ghost_inbox` 时控制 fail closed，避免冷启动或错误组装意外把 Ghost 文本
  送入 provider。
- parity 观察保持不变，说明显式开启控制的 fixture 保留了旧行为。
- 这是离线结构和 provider-send 边界实验；没有 KoboldCpp live/offline 模型行为
  对照、成本/延迟测量或跨模型结果。
- 全量测试没有留下未分类失败；durable 修复只保证初始化锁顺序和 schema 观察
  一致性，不扩大 Ghost 的 prompt 权限或写入路径。

## 当前生产边界

- 是否经过真实 provider-send：经过 shared kernel fake provider-send capture；未经过 KoboldCpp
- 是否改变默认 prompt：正常生产 state 且控制开启时不改变；缺失控制时 fail closed
- 支持的模型 / 任务 / 配置：所有使用真实 `TaskRunDeps` 的 research runtime；不宣称跨模型收益
- 生产合并结论：`SHADOW_ONLY`

## 已知问题和回滚方式

- 0.6.1 仍缺 KoboldCpp live smoke、故障注入报告和完整 provider 矩阵；0.6.2
  仍缺完整 reset manifest、runtime 并发矩阵和成本/延迟测量。
- 旧的外部 fixture 若直接构造无 state 的 research deps，会立即暴露契约错误；应
  补齐 state，不应恢复生产 fallback。
- 回滚 `41167a7a` 会恢复缺失 state 的隐式兼容行为；`23e4a4b6` 只包含 fixture
  对齐和回归测试，可独立回滚但不应单独留下无 state fixture。

## 下一步

1. 在 shared kernel/provider capture 上完成 0.6.1 的 live smoke 和故障注入；KoboldCpp
   故障按交接流程停止重试并标记 `BLOCKED`。
2. 完成 0.6.2 的 scope/delete/reset/restart/concurrency acceptance battery。
3. 在通过 admission/control 闭环后，再进入 language shadow；World Model 继续保持
   bounded review shadow，不进入默认 prompt。
