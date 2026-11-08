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
- 三批代码与验证已完成；第三批提交标题为 `refactor(types): define checkpoint port and guard typed boundaries`。

## 第一批：任务入口与恢复

已完成并提交：`376fe2a7 refactor(types): tighten task entry and policy recovery contracts`。

- `build_task_policy_for_entry` 使用 TaskSubmission → TaskPolicy；RunFrame 保存 TaskPolicy / TaskSession。
- `start_task_session` 直接读取领域字段与执行证据；保留策略一致性检查、轮次预算归一化。
- TaskSubmission 没有 coding_context_enabled，统一入口沿用 TaskSession 的 True 默认，不继续探测不存在的字段。
- `_entry_policy_with_recovery` 显式接收 DurableSession | None，不再接收动态 deps；恢复、策略记录辅助函数消除显式动态类型。
- 迁移缺字段请求夹具；新增会话复用、拒绝更换授权、预算与证据传递回归。
- 第一轮新增测试两项失败：夹具指纹不是合法 SHA-256 值，被现有生产校验清空。修正测试输入，未放宽生产校验。

## 第二批：写入回调与静态导入

已完成并提交：`0c9fa774 refactor(types): type checkpoint callbacks and preserve retry inputs`。

- RuntimeMutationLine.durable 使用 DurableSession；全部 mutation 回调明确 RuntimeProjection、RuntimeLogEntry、RuntimeRows。
- checkpoint 的 derive_rows、boundary_transition、boundary_result 与返回的 SubmissionRecord 同步标注。
- 三处 importlib 动态模块调用改为函数内静态导入，保留延迟加载。
- provider/tool/delivery/publication builders 与 SessionView 的投影参数消除显式动态类型。
- 发现并复现确定性生产缺陷：begin_tool_batch 接收一次性迭代器，CAS 冲突后回调重跑时意图已消耗，成功提交的批次会丢失工具效果。已在回调外固定为 tuple。
- 新增 test_checkpoint_mutation_contract：Memory / SQLite × 一次冲突、两次冲突、三次重试耗尽。修复前 4 failed / 2 passed，失败明确为成功重试丢失 effect_ids；修复后全部通过。
- 范围说明：本批消除写入接线与回调中的动态类型；checkpoint 的历史行字典/快照解析仍有旧动态注解，不宣称全仓库或整个 checkpoint 已清零。

## 第三批：durable Protocol 与精度保护

已完成，随本批提交。

- RuntimeCheckpointPort 定义 task/head/artifact/transact_task/merge_generation_boundary；写入层与 checkpoint 两个接收函数统一依赖该协议。
- RuntimeRows / RuntimeRowsBuilder 合并到已有 session_projection 模块，避免重复定义与循环依赖。
- 架构白名单明确允许 codey.durable.contracts，durable 内核仍可独立导入。
- 委托实现不继承 DurableSession，只实现协议能力；Memory / SQLite 验证工件写入、final 边界原子放置并重开写入、取消时无新提交。
- 协议测试初版越过 writer_running → closing 的合法步骤，修正测试流程而未放宽生产状态机。
- 精度门禁新增 8 个完整文件的零动态类型保护，以及 task_entry / context / checkpoint 的已迁移符号保护；新增符号缺失、别名、嵌套回调、完整 Callable 签名的检测回归。
- 精度门禁的首版回调检测误取 AST Subscript 自身名称，新增回归暴露后修复为检查其基类型名称。
- 额外对新增两份 durable 测试运行 mypy：发现一个读取 object 直接索引的测试错误，使用 Mapping 收窄后检查通过。
- 已更新 docs/typechecking.md，明确保护范围与剩余历史动态类型。

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

第三批验证：

- 直接相关 pytest（runtime_checkpoint_port、checkpoint_mutation_contract、kernel_type_precision_gate、runtime_transaction_owner、kernel_boundary、generation_boundary_commit、provider_sink_generation_boundary、recovery_requires_original_policy、architecture）：178 passed、464 subtests passed。
- `python -m ruff check codey tools tests`：通过。
- `python -m mypy codey --python-version 3.11`：646 files，通过。
- `pyrefly check codey --python-version 3.11`：0 errors，保留既有 suppressed / warnings。
- `ty check codey --python-version 3.11`：通过。
- `python -m mypy tests/durable/test_runtime_checkpoint_port.py tests/durable/test_checkpoint_mutation_contract.py --python-version 3.11`：2 files，通过；同时检查委托实现与协议的结构兼容性。
- `python -m tools.kernel_type_precision_gate`：包括新增整文件、命名符号、完整回调保护，检查通过。

## 完成核对与边界

- 入口与恢复输出明确 TaskPolicy，frame 明确保存 TaskPolicy / TaskSession；继续任务策略不可更换。
- RuntimeMutationLine 全文件无显式动态类型；checkpoint 的回调、durable 依赖及边界放置结果具有明确类型。
- Protocol 覆盖适配器的完整能力需求，真实 DurableSession 及委托实现均通过类型检查。
- 一次性迭代器重试丢失意图的生产缺陷先复现后修复；两个存储后端验证成功重试只提交一次，重试耗尽不留下部分事实。
- 对照开始时 HEAD，12 个变更生产文件的 AST `Any` 标识符引用由 132 降至 53，共移除 79；每个文件都未增加。其余引用在 task_entry.py（41）、context.py（8）、checkpoint.py（4）的旧边界，未伪称全项目动态类型清零。
- 未新增生产 cast、类型忽略、非零精度基线或关闭检查目标；没有添加运行时协议探测与兼容兜底。
- 按约束未运行完整 pytest、覆盖率报告、安装包构建或安装包测试；验证聚焦实际改动路径。
