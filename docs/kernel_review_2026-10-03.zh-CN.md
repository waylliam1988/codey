# 提交后审查：边界校验与冷启动清理

日期：2026-10-03。审查基线：`96ce456e..2be380c1`，本次从干净的
`2be380c1` 工作区开始。覆盖后续七个提交的依赖分组、全树类型化、
任务提交资源、测试卫生和本地模型测试门，并检查相关生产调用者。

## 结论

主干继续收敛：网页 JSON 与原生协议仍使用共同循环、执行与完成边界。
新增请求分组有清晰职责，但保留旧关键字入口、再复制一份扁平请求没有必要，
本次已删除这些生产兼容层。此次可复现的问题已经修复。

生产 Python 代码净减少 **265 行**；`task_loop.py` 从 **1051 → 879 行**。
`run_task_kernel` 从 **172 → 165 行**，Ruff 计算圈复杂度为 **16**，没有复杂度豁免。
它串联快照、接收、完成检查、执行、交付，保留可见的顺序；本次没有机械拆分该流程。

这些结果不能证明全库没有任何 bug 或死代码。全树 mypy 零错误也不表示
所有动态数据、未标注函数体和外部服务都已得到形式化验证。

## 复现的问题与正式所有者

| 问题 | 原因与影响 | 修复 |
| --- | --- | --- |
| 效果身份及恢复轮次被洗成合法整数 | 非法字符串、浮点数、布尔值被归零或转换，可能与合法轮次身份重合 | `task_session.effect_coordinates` 检查精确整数及非负范围；`turn_effect_id`、`kernel_execution._turn_setup` 共用；内核启动前检查恢复轮次 |
| 队列 claim 计数先转换再校验 | `"1"`、`1.0`、`1.9` 可成为合法 lease claim | `work_queue_events` 对原始计数校验；校验与应用均拒绝非法输入，不改状态 |
| 账本计数先归零再校验 | 负数被归零；布尔值被当整数；`True == 1` 使数字列表通过规范性比较 | `evidence_ledger` 检查原始计数的精确类型和范围；页码、引用编号及汇总计数同样检查 |
| 错误上下文预算降到最小值 | 类型化迁移后的转换器吞掉异常，外层默认 8000 的分支不再执行 | `evidence_rules._context_char_limit` 显式传默认 8000；合法小值仍按最小 2000 限制 |
| 测试矩阵误报完整 | 集合只检测缺项，忽略重复、额外轮次，并转换 attempt 类型 | `local_model_gate_attempts._attempt_matrix` 按原始身份计数，要求每个预期槽位恰好一次，无额外、重复或非法项 |
| 终止确认被当成任务截断 | 原生工具结果的单 token 确认也可能返回 length | `_provider_metrics` 根据对应请求分开 active/terminal finish reasons；诊断仅用 active，原始总列表保留用于审计 |

## 删除与测试迁移

- 删除生产 `KernelRunRequest.from_legacy`、`_BoundRunRequest`、`_bind_run_request`
  和 `**legacy_options`；入口只接收正式 `KernelRunRequest`。
- 迁移 33 个测试/压力测试文件中的 70 处直接调用，显式构造 transport、execution、
  observation 分组；没有增加测试侧旧参数适配器。AST 比较确认原有 **857 个**
  pytest/unittest 断言节点保持一致。
- 删除 28 个重复数值异常处理分支：转换器已处理这些异常，外层不再重复接管。
  保留真正需要的输入边界默认值和 I/O 错误处理。
- 删除 `AppContext` 中不可达的全局临时目录兜底；原生 schema 排序直接读取
  本轮自行构造的字段。没有新增缓存、循环或通用框架。
- 内核文件降到 1000 行以下，移除该文件的大文件豁免和旧上限。
- Windows/Linux CI 都改为 `python -m mypy codey`；删除重复的 16 模块迁移清单。

## TDD 与测试卫生

新增六个测试文件及一个边界删除锁，合计 **65 条新增参数化场景/用例**。
分组行为红测累计 **59 条失败**，另有 **6 条正例**；随后修绿并回测。
测试编写期间的夹具、语法准备错误不计入行为红测。

- `test_effect_identity_rejects_invalid_coordinates.py`：27 条，覆盖效果身份、
  执行前拒绝、恢复启动前拒绝及合法身份。
- `test_work_queue_claim_preserves_invalid_retry_count.py`：5 条，覆盖实际重放、
  校验与应用，不仅检查返回字段。
- `test_evidence_ledger_counts_validate_before_normalization.py`：17 条，包含
  从真实 Store 生成合法账本后只改变目标字段的反例。
- `test_research_context_budget_invalid_value_uses_default.py`：5 条。
- `test_local_gate_matrix_and_terminal_metrics.py`：9 条，包含真实 JSONL 请求/回复配对。
- `test_ci_full_tree_typing_gate.py`：1 条，锁定 Windows/Linux 全树门槛。
- `test_kernel_dependency_boundary.py`：新增 1 条，锁定正式签名及兼容载体删除。

全量暴露旧 `test_work_queue_transition_overflow` 只检查源码是否包含
`OverflowError`。原实现不再转换整数，该文本检查失效。
已替换为真实非法输入、不执行转换、不改原对象和合法整数仍可转换的行为检查。
同文件的一条恒真自比较断言改为固定期望值。该文件仍有历史结构检查，
文档已明确它们不等于行为证明；未根据这些检查宣称全库安全。

## 验证结果

- 最终全量：`RUN_BROWSER_E2E=1 python -u -m pytest -q -o faulthandler_timeout=120 -rs`
  → **6734 passed、6 skipped、1497 subtests passed，444.67 秒**，零失败。
- 实际运行真实 Edge 项目流程。6 个跳过均为 Windows 不支持的 POSIX
  权限、进程组、路径或 `O_NOFOLLOW` 契约；没有跳过发现的问题。
- 前一完整全量：**6733 passed、1 failed、6 skipped、1488 subtests passed，458.20 秒**。
  唯一失败为上述源码文本断言。更早的一次预备全量在补充账本汇总计数反例时主动中止，
  不作为验收结果。
- Python **3.13.15** 针对性复验：进程捕获、身份、账本、矩阵和队列，
  **86 passed，5.38 秒**。这是针对性测试，不是第二份全量证明。
- Ruff、compileall、`git diff --check` 通过；mypy **363 个源码文件零错误**。
- 保留此前文档中的失败基线，没有改写历史实验或测试结果。

## 数学和体验结论的范围

新增边界使用直接谓词：只有精确 `int` 且非负的效果坐标进入身份构造；
规范账本的计数只允许精确整数；矩阵完整意味着每个预期槽位计数为 1，
额外槽位和非法观察不存在。这些局部性质可以按代码分支证明，测试提供反例锁。
哈希身份的稳定性测试不构成对任意输入无碰撞的数学证明。

已有有限状态遍历仍通过，它检查声明的有限抽象及不变量；
它没有覆盖所有自然语言目标、模型输出、外部网络或全部线程调度。
因此没有引入一个号称能证明整个程序无 bug 的新框架。

本次未新增真实模型成功率、token、延迟或内存基准。可以确认冗余接线减少、
边界更严格、真实 Edge 回归通过；不能据此宣称用户体验或运行性能已全面改善。
