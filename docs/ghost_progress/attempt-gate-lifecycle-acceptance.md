# Ghost 实验：Attempt Gate Lifecycle Acceptance

- 实验名称：Attempt Gate Lifecycle Acceptance
- 状态：`EXPERIMENT_COMPLETE`
- 负责人：Codex
- 开始日期：2026-10-22
- 最后更新：2026-10-22
- 关联 Roadmap 验收门：0.6.1 Context Epoch admission
- 实验 commit：`b887fc90` (`test(ghost): cover attempt gate lifecycle paths`)
- 生产合并结论：`EXPERIMENT_ONLY`

## 本次目标和假设

验证同一个生产 `ApiProvider` Attempt Gate 是否覆盖 0.6.1 尚未形成统一证据的发送生命周期：
native tool result follow-up、native 错误 receipt、从 durable context 恢复后的下一次发送、
compaction auxiliary provider，以及不产生物理 HTTP 的 terminal receipt。假设所有实际 HTTP
attempt 都重新经过 gate；撤回应在 transport 前变成 `RequestPrepError`，且不会把无 HTTP 的
本地 receipt 误记成一次发送。

## 实际改动

- 新增 `tests/test_ghost_attempt_gate_lifecycle.py`，直接调用生产 `ApiProvider`、API codec、
  `repair_native_dangling` 和 attempt gate；没有新增生产 fallback、第二套 validator 或 prompt 拼接。
- 两个 API 协议（Completions / Responses）都覆盖 native follow-up 和错误 receipt；恢复与
  compaction 使用生产 provider 状态绑定。

## 验收命令和结果

```powershell
python -m pytest tests/test_ghost_attempt_gate_lifecycle.py -q --tb=short
python -m ruff check tests/test_ghost_attempt_gate_lifecycle.py
python -m mypy tests/test_ghost_attempt_gate_lifecycle.py --python-version 3.11
pyrefly check tests/test_ghost_attempt_gate_lifecycle.py --python-version 3.11
ty check tests/test_ghost_attempt_gate_lifecycle.py
```

结果：`7 passed`；Ruff、mypy、Pyrefly（`0 errors`）和 ty 全部通过。

## 实验配置

- 模型 / suite / endpoint：无模型；确定性 `api_transport.generate` fixture；无外部 endpoint。
- tasks / cells / reps：native follow-up 两协议 2 cells；native error receipt 两协议 2 cells；
  durable resume 1 cell；compaction auxiliary 1 cell；terminal receipt 1 cell；每 cell 1 次。
- on/off 对照：第一次 gate 允许、第二次 gate 撤回；terminal receipt 与 physical generation
  的有/无对照；恢复前后的同一 payload 来源对照。

## 结果与限制

- native follow-up 和错误 receipt 的第二次 gate 均在 transport 前拒绝，实际 physical payload
  只产生第一条；两个 API 协议结论一致。
- 恢复上下文的下一次发送仍由同一 gate 检查，并保留 durable history；compaction auxiliary
  继承 gate，且 purpose 保持为 `compaction`。
- terminal receipt 只提交本地协议历史，不生成 physical attempt，因此不虚增 gate/HTTP 计数。
- 这是生产 provider boundary 的确定性离线证据，不是 KoboldCpp 模型行为、成本或延迟结果；
  不足以单独完成 0.6.1 的完整 live/off、repair/resume/compaction acceptance。

## 当前生产边界

- 是否经过真实 provider-send：经过真实 `ApiProvider` generation boundary；transport 使用 fixture，
  没有外部模型服务。
- 是否改变默认 prompt：否。
- 支持的模型 / 任务 / 配置：两个 OpenAI 协议的本地 provider lifecycle；无模型收益结论。
- 生产合并结论：`EXPERIMENT_ONLY`。

## 已知问题、回滚和下一步

- 仍缺完整真实 runtime 的 repair/resume/compaction acceptance、scope 并发矩阵、live/off 对照、
  成本/延迟和 KoboldCpp payload fault injection；当前 0.6.1 保持 `INCOMPLETE`。
- 回滚本实验只需回滚 `b887fc90`；它不改变生产代码、默认 prompt 或 Ghost store。
- 下一步继续补齐真实 task kernel 入口上的 recovery/repair/compaction 组合矩阵，再决定是否
  有足够生产收益推进更窄的 shadow runtime。World Model 继续保持 `SHADOW`，不因本实验启动。
