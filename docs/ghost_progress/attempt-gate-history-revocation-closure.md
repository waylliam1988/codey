# Ghost 实验：Attempt Gate History / Revocation Closure

- 实验名称：Attempt Gate History / Revocation Closure
- 状态：`EXPERIMENT_COMPLETE`（限定 loopback provider-send 边界）
- 负责人：Codex
- 日期：2026-10-21
- 关联 Roadmap 验收门：0.6.1 Context Epoch admission；0.6.2 `admit_hints` 即时生效
- 实验 commit：生产接线 `3d025cce`；当前复验 `bfac20d8`
- 生产合并结论：`PRODUCTION_MERGED`（仅本实验覆盖的唯一 Attempt Gate 行为）

## 本次目标和假设

此前两个结构探针分别证明：source expiry 会在后续 history 中重发 hint，首轮后关闭
`admit_hints` 也会重发旧 hint。目标是验证唯一 provider Attempt Gate 在 codec 生成完整
payload、lease/retry 之前的每个物理 HTTP attempt 上重新读取 admission，且 text/native 两个
协议都拒绝过期或已撤回的历史 hint。

## 实验配置

- 模型 / suite / endpoint：`synthetic-history-expiry` / `ghost_history_expiry_repro_20261021`；
  `synthetic-control-revocation` / `ghost_control_revocation_repro_20261021`；临时 loopback HTTP。
- tasks / cells / reps：
  - expiry：read fixture → done；text/native × expiring/not-expiring，4 cells，各 1 次；
  - revocation：read fixture → done；text/native × enabled/disabled_initially/disabled_after_first，
    6 cells，各 1 次。
- 配置：context 32768、reserve 8192、keep recent 12000、output 128、temperature 0.3、
  timeout 5s、max turns 2。
- on/off 对照：不过期/过期；一直开启/初始关闭/第一条 HTTP 后关闭。没有 KoboldCpp，
  没有模型行为评分。

## 验证命令和结果

```powershell
python tests/manual/ghost_memory_routing/history_expiry_probe.py `
  --suite ghost_history_expiry_repro_20261021 `
  --metrics-out artifacts/ghost_memory_routing/history-expiry-repro-20261021.json
python tests/manual/ghost_memory_routing/control_revocation_probe.py `
  --suite ghost_control_revocation_repro_20261021 `
  --metrics-out artifacts/ghost_memory_routing/control-revocation-repro-20261021.json
python -m pytest tests/test_provider_attempt_gate.py tests/test_ghost_provider_send_boundary.py `
  tests/durable/test_context_edit_folding.py tests/durable/test_context_edit_revision_fence.py `
  tests/durable/test_conversation_plan_no_provider_export.py tests/test_ghost_control_contract.py `
  tests/test_ghost_controls.py -q --tb=short
```

结果：expiry 4/4 完整；不过期 text/native 各 2 次 HTTP，过期 text/native 各 1 次并在
HTTP 前 `provider_failure` 拒绝，`expired_hint_sent=false`。revocation 6/6 完整；初始关闭
两项不发送 hint，首轮后关闭两项均在第二次 HTTP 前明确拒绝，`revoked_hint_resent=false`，
一直开启两项保留 hint。重点回归测试 `134 passed`。

脱敏指标：

- [expiry](../../tests/manual/ghost_memory_routing/reports/attempt-gate-history-expiry-20261021.json)
- [revocation](../../tests/manual/ghost_memory_routing/reports/attempt-gate-revocation-20261021.json)

## 当前生产边界

- 是否经过真实 provider-send：是；实际 task entry、builder、kernel、ApiProvider 和 loopback HTTP。
- 是否改变默认 prompt：不改变；只在发送前拒绝不再有资格进入 payload 的历史 hint。
- 支持的模型 / 任务 / 配置：ApiProvider Completions/Responses；合成 read→done 任务；不作模型收益推广。
- 生产合并结论：`PRODUCTION_MERGED`，仅限唯一 Attempt Gate 的 expiry/revocation 边界。

## 收益、限制和阴性结果

收益是已复现的历史过期和运行中撤回不会继续发送到 loopback HTTP。旧的负结果报告仍保留，
用于证明修复前的行为；本实验没有 live 模型收益、成本、延迟或跨模型结论，也没有证明远端
网页 provider 的不可见 history 可被 Codey 重新裁决。

## 未完成或阻塞

- 0.6.1 仍缺 repair、receipt、resume、compaction、完整 scope/expiry matrix、live/off 对照及成本/延迟证据。
- 0.6.2 仍缺 reset/delete manifest、runtime 并发 acceptance 和完整四开关 battery。
- 远端 `origin/master` 尚未与当前本地 `master` 同步；本记录只依据本地 commit。

## 下一步

沿同一个 Attempt Gate 扩展 normal send、native result、repair、receipt、resume、compaction
和拒绝后的恢复矩阵；完成后再评估 0.6.1/0.6.2 是否具备整体晋级证据。World Model 继续
保持 `SHADOW`，不进入默认 prompt。
