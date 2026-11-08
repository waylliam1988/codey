# 任务入口与 checkpoint 类型收紧进度

## 目标与约束

落实目标文件中的三批修改，分别提交：

1. 任务入口、RunFrame、TaskSession 接线与策略恢复使用明确类型；迁移测试夹具。
2. RuntimeMutationLine 使用明确 durable 类型，checkpoint 回调完整标注，动态导入改为可静态检查的延迟导入；消除相应辅助函数的动态类型。
3. 提取 RuntimeCheckpointPort，统一写入层和 checkpoint 适配器的能力依赖，维护架构约束和精度保护。

不新增 Any、类型忽略或掩盖问题的 cast；不增加防御性冗余。发现确定性生产缺陷时补回归并修复。优先直接相关测试，不运行完整 pytest、覆盖率或安装包构建。类型检查按项目要求串行执行。

## 当前状态

- 开始时 HEAD：`b9e8d19a`；工作区干净。此前聊天中的未提交修改已经在当前 HEAD 中提交，不属于本次修改。
- 未发现适用的 AGENTS.md。
- 已重新读取目标附件；之前只提供教程，本轮开始落实代码。

## 第一批：任务入口与恢复

已完成并提交：`376fe2a7 refactor(types): tighten task entry and policy recovery contracts`。

- `build_task_policy_for_entry` 使用 TaskSubmission → TaskPolicy；RunFrame 保存 TaskPolicy / TaskSession。
- `start_task_session` 直接读取领域字段与执行证据；保留策略一致性检查、轮次预算归一化。
- TaskSubmission 没有 coding_context_enabled，统一入口沿用 TaskSession 的 True 默认，不继续探测不存在的字段。
- `_entry_policy_with_recovery` 显式接收 DurableSession | None，不再接收动态 deps；恢复、策略记录辅助函数消除显式动态类型。
- 迁移缺字段请求夹具；新增会话复用、拒绝更换授权、预算与证据传递回归。
- 第一轮新增测试两项失败：夹具指纹不是合法 SHA-256 值，被现有生产校验清空。修正测试输入，未放宽生产校验。

## 第二批：写入回调与静态导入

已完成，准备提交。

- RuntimeMutationLine.durable 使用 DurableSession；全部 mutation 回调明确 RuntimeProjection、RuntimeLogEntry、RuntimeRows。
- checkpoint 的 derive_rows、boundary_transition、boundary_result 与返回的 SubmissionRecord 同步标注。
- 三处 importlib 动态模块调用改为函数内静态导入，保留延迟加载。
- provider/tool/delivery/publication builders 与 SessionView 的投影参数消除显式动态类型。
- 发现并复现确定性生产缺陷：begin_tool_batch 接收一次性迭代器，CAS 冲突后回调重跑时意图已消耗，成功提交的批次会丢失工具效果。已在回调外固定为 tuple。
- 新增 test_checkpoint_mutation_contract：Memory / SQLite × 一次冲突、两次冲突、三次重试耗尽。修复前 4 failed / 2 passed，失败明确为成功重试丢失 effect_ids；修复后全部通过。
- 范围说明：本批消除写入接线与回调中的动态类型；checkpoint 的历史行字典/快照解析仍有旧动态注解，不宣称全仓库或整个 checkpoint 已清零。

## 第三批：durable Protocol 与精度保护

待开始。

## 验证记录

第一批验证：

- 直接相关 pytest：test_entry_session_task_requirements_init、test_entry_authorization_cannot_expand_by_route、test_entry_auth_denied_capabilities、test_approval_authorization_recovery、test_recovery_requires_original_policy、test_task_entry_kernel_prod、test_task_entry_operation_state、test_research_pipeline：111 passed，6 subtests passed。
- `python -m ruff check codey tools tests`：通过。
- `python -m mypy codey --python-version 3.11`：646 files，通过。
- `pyrefly check codey --python-version 3.11`：0 errors（既有 suppressed / warnings 不代表零动态类型）。
- `ty check codey --python-version 3.11`：通过。
- `python -m tools.kernel_type_precision_gate`：通过。

第二批验证：

- 直接相关 pytest（checkpoint_mutation_contract、runtime_transaction_owner、operation_driver_restart、provider_sink_generation_boundary、generation_boundary_commit、research_continuation_restart、kernel_boundary、durable_sink_single_settlement_fail_closed、edit_bump_failure_single_settlement_durable、runtime_effect_records、architecture）：156 passed、2 skipped、464 subtests passed。
- 跳过项为既有测试，不以跳过证明行为。
- `python -m ruff check codey tools tests`：通过。
- mypy / Pyrefly / ty 全量生产目录，目标 Python 3.11：全部通过；mypy 646 files，Pyrefly 0 errors。
- kernel_type_precision_gate 与 git diff --check：通过。
