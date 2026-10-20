# Ghost 实验进度记录：Frozen Evaluation and Ownership Audit

- 实验名称：Frozen Evaluation and Ownership Audit
- 状态：EXPERIMENT_COMPLETE
- 负责人：Codex
- 开始日期：2026-10-21
- 最后更新：2026-10-21
- 关联 Roadmap 验收门：0.6.0
- 实验 commit：`250a1fea` (`test(ghost): freeze evaluation and ownership audit`)
- 生产合并结论：EXPERIMENT_ONLY

## 本次目标

冻结 Ghost 0.6.0 的最小可复查评估矩阵和所有权边界。假设是：先把缺失模型、缺失 off 对照和不足样本明确标成 `INCOMPLETE`，并修正
`PROMOTE-MATCHED` 的排序映射，才能避免把历史 manual harness 结果误写成生产晋级。

## 实际改动

- 新增 `tests/manual/ghost_memory_routing/evaluation_contract.py`，冻结 modelA 单模型试点、4 个问法、每问法 5 次、每 cell 至少 20 次、language/command on/off 对照和缺失数据规则。
- `round4.py report` 改用冻结 evaluator；缺少必需 cell 或样本时输出 `INCOMPLETE`，不会把缺失当成 0；`PROMOTE-MATCHED` 在聚合时保持 rank 2。
- 提交脱敏配置快照 `tests/manual/ghost_memory_routing/reports/evaluation_freeze.json`。
- 添加 `tests/test_ghost_evaluation_contract.py`，覆盖配置、样本门槛、缺失 suite 和 rank 映射。

## 验收命令与结果

- `python tests/manual/ghost_memory_routing/summarize.py`：通过；历史快照 232 条 R4 记录可复算。
- `python tests/manual/ghost_memory_routing/round4.py report`：通过；modelA language 为 19/20 vs 0/20，command 为 0/20 vs 0/20；完整性为 `COMPLETE`。
- `python -m pytest tests/test_ghost_evaluation_contract.py -q`：通过（4 tests）。
- `python tests/manual/ghost_memory_routing/round4.py offline`（`GHOST_EXPERIMENT_HOME=.tmp-ghost-0.6.0-20261021` 新隔离目录）：通过；parity `strict_text_equal=true`（580 bytes），冲突删除/重建 `no_revival_ok=true`，expiry `expired_lines=0`，trigger false positives 为 0。
- `python tests/manual/ghost_memory_routing/round3.py t5`（同一隔离目录）：`supersede_ok=false`（nodes_language=2，Chinese/English 均未满足单节点替换条件）。这是现有 T5 fixture 的阴性结果，不能改 scorer 或算作模型行为阴性；已保留在本地 state，后续单独修复生命周期语义。
- 直接使用迁入的旧归档运行 `round4.py offline` 仍会抛 `SchemaVersionError`；该故障只说明旧 state 与现行 schema 不兼容。

## 实验配置

- model / suite / endpoint：`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M` / `modelA` / 历史 KoboldCpp endpoint（原始 endpoint 仅保留在本地归档）
- tasks / cells / reps：language 与 command 各 4 个问法；`lang_on_d2`、`lang_off_d2`、`cmd_on_d2`、`cmd_off_d2`；每问法 5 次，每 cell 20 次
- on/off 对照：仅改变对应 Ghost slot 是否投递；任务、模型和 scorer 固定

## 所有权和 admission 绕过审计

| 路径 | 当前角色 | 0.6.0 结论 |
|---|---|---|
| `codey/ghost/*` stores/events | Ghost 写入本地 observation、projection 和 control event | 只负责本地状态与投影；不写 Evidence、Permission、CompletionProof |
| `codey/operations/ghost_context.py` | runtime 读取 directive/continuity projection | 只读消费；不拥有 provider send |
| `codey/agents/context.py` | 将模型可见 sections 组装为 `PromptEnvelope` | 候选渲染 owner；0.6.1 需由 admission validator 最终裁决 |
| `codey/runtime/observe/prompt_envelope.py` | 记录 provider-send prompt section、digest、epoch | 当前是 trace/projection，不等于准入 gate |
| `codey/workspace/context_epoch.py` | 计算内容寻址 epoch 和 bounded admission record | 纯函数、无 I/O；当前缺真实 send 前拒绝 gate |
| `codey/operations/kernel_transport.py` / `task_loop.py` | 真实 provider send | provider-send owner；0.6.1 需要在此之前验证 envelope |
| `codey/operations/prompting.py` | 历史字符串拼接 helper | Ghost-bearing 直接拼接是 0.6.1 绕过候选，当前不扩功能 |
| `codey/app/cli.py` Ghost preview | CLI 预览 | 不属于 provider-send；不能作为真实 admission 证据 |
| `codey/agents/consensus.py`、`app/review_service.py`、`operations/conversation_plan.py` | 其他 provider-send 路径 | 当前不注入 Ghost；需在 0.6.1 明确是否复用同一 validator |

写权限只属于 runtime observation/event producer 与 Ghost 自己的本地 stores；projection、prompt envelope 和 Context Epoch 对下游只读。`PromptEnvelope` trace 不能替代 admission，World Model 尚未接入。

## 结果与限制

冻结规则和历史 modelA 数据满足 0.6.0 的离线完整性门，支持“modelA 条件试点”的复查。language 的 19/20 只证明受测配置下的候选收益；command 0/20 没有收益。单模型没有跨模型结论；R4 parity/conflict/expiry 离线检查已通过，但 T5 supersede fixture 仍是阴性结果。

## 已知问题和回滚方式

- 这是 manual harness 和文档实验，不改变默认 prompt、生产 builder 或 provider-send。
- 若 evaluator 判定不符合预期，可回滚本实验 commit；历史报告和原始归档不改写。
- `round4.py offline` 需要在隔离的 `GHOST_EXPERIMENT_HOME` 下重建 R4/T5 fixture；R4 离线检查已通过，但 T5 supersede 仍为阴性，不能把旧 state schema 或该 fixture 故障算作模型阴性。

## 下一步

1. 在新隔离目录重建 deterministic parity/conflict/expiry fixture，完成 0.6.0 离线生命周期证据。
2. 进入 0.6.1：让 Ghost `ContextSource → PromptEnvelope → Context Epoch admission → provider send` 在真实捕获路径中拒绝绕过、超预算和关闭 source。
3. World Model 继续保持 `EXPERIMENT_ONLY/SHADOW`，先实现事件、prediction review 和 bounded projection；在有真实消费者前不进入默认 prompt。
