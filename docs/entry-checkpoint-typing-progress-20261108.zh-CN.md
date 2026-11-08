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

已完成，准备提交。

- `build_task_policy_for_entry` 使用 TaskSubmission → TaskPolicy；RunFrame 保存 TaskPolicy / TaskSession。
- `start_task_session` 直接读取领域字段与执行证据；保留策略一致性检查、轮次预算归一化。
- TaskSubmission 没有 coding_context_enabled，统一入口沿用 TaskSession 的 True 默认，不继续探测不存在的字段。
- `_entry_policy_with_recovery` 显式接收 DurableSession | None，不再接收动态 deps；恢复、策略记录辅助函数消除显式动态类型。
- 迁移缺字段请求夹具；新增会话复用、拒绝更换授权、预算与证据传递回归。
- 第一轮新增测试两项失败：夹具指纹不是合法 SHA-256 值，被现有生产校验清空。修正测试输入，未放宽生产校验。

## 第二批：写入回调与静态导入

待开始。

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
