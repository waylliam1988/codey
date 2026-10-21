# Ghost 实验：Provider Control Revocation

- 实验名称：Provider Control Revocation
- 状态：EXPERIMENT_COMPLETE（负结果基线与当前 Attempt Gate 闭合均已记录）
- 负责人：Codex
- 日期：2026-10-21
- 关联 Roadmap 验收门：0.6.1 / 0.6.2 即时生效
- 实验 commit：`2db5f328c20c588f549550c26e9975a7545b621a`（负结果探针）；
  `c39f0897c74b6d3c980c1ca75ed9894ea1c474a0`（旧实现负结果）；闭合生产 commit `3d025cce`；
  当前复验 commit `bfac20d8`
- 生产合并结论：PRODUCTION_MERGED（仅当前 Attempt Gate 的撤回边界）

## 目标和假设

验证首轮有效发送 hint 后，真实 GhostInboxStore 的 session `admit_hints` 被关闭，
第二次完整 HTTP payload 是否仍携带它。假设 task entry 只取一次控制快照，
后续发送不能发现撤回；旧 history 也不能由仅检查新 prompt 的 gate 排除。
这是控制即时生效的结构实验，不测模型语言收益。

## 实验配置

- 模型 / suite / endpoint：synthetic-control-revocation /
  provider_control_revocation_20261021_final / 临时 loopback HTTP，无 live 模型。
- tasks / cells / reps：一个实际 read_file→done 任务；text/native × 开启 / 初始关闭 / 首轮后关闭，6 项各一次。
- on/off 对照：固定 synthetic directive candidate，真实 AppContext、scoped GhostInboxStore、
  task entry、workspace builder、kernel 和 ApiProvider；只有控制转换时点变化。
- 初始关闭必须首轮无 hint；一直开启必须第二轮仍保留 hint。
- 首轮后关闭发生在第一条 HTTP request 被捕获之后、返回 read_file 响应之前；读取实际生效值。
- context 32768 / reserve 8192 / keep recent 12000 / output 128 / temperature 0.3 / timeout 5s / max turns 2。
- 不改 scorer、不以缺失项为零；异常立即保存 cell 摘要并停止，不重试。
- 原始 request/reply、state、events、manifest 只存 ignored artifacts；只提交脱敏指标。

## 验证命令和结果

```powershell
python tests/manual/ghost_memory_routing/control_revocation_probe.py --suite provider_control_revocation_repro --metrics-out artifacts/ghost_memory_routing/control-revocation-repro-metrics.json
python -m ruff check tests/manual/ghost_memory_routing/control_revocation_probe.py
```

首次保存报告的命令使用 `--suite provider_control_revocation_20261021_final` 与
`--metrics-out tests/manual/ghost_memory_routing/reports/control-revocation-metrics.json`。
复现须用新 suite，已有目录拒绝覆盖。exit 0 仅表示矩阵完整，不能作为控制 gate 通过结论。

结果：6/6 完整、全部 done、每项两次 HTTP，共 12 次合成 HTTP。
一直开启两项均保留 hint；初始关闭两项的两次请求均无 hint；首轮后关闭两项均仍在第二次
payload 重发 hint。真实 store 确认 session disable 写入成功、effective=false；runtime
每项都只在 entry 读取一次 admit_hints。实验的转换确认使用未包装的真实 reader，单独记入
control_transitions，不冒充 runtime 再读取。

初版 suite `provider_control_revocation_20261021_recorded` 也完成 6 项；发现 runtime read
列表混入实验自己验证开关的 read，因此修正仪表并用新 suite 完整运行，旧归档保留，
不重复累计样本或把仪表修正称为生产变化。两版均未发生 KoboldCpp 调用/故障。

[脱敏指标](../../tests/manual/ghost_memory_routing/reports/control-revocation-metrics.json)；
[离线报告](../../tests/manual/ghost_memory_routing/reports/control-revocation.md)。
原始 manifest、state、requests、events、results 只在 ignored
`artifacts/ghost_memory_routing/provider_control_revocation_20261021_final/`。

## 当前生产边界

- 是否经过真实 provider-send：是，实际 task entry → builder → kernel → ApiProvider → loopback HTTP。
- 是否改变默认 prompt：否；仅本地 synthetic candidate fixture。
- 支持模型 / 任务 / 配置：受测合成响应；不作模型效果推广。
- 生产合并结论：`PRODUCTION_MERGED`，仅表示当前唯一 Attempt Gate 已在 loopback HTTP
  上闭合本实验的首轮后撤回缺口；不表示 0.6.2 整体验收完成。

## 当前 commit 上的闭合复验

`bfac20d8` 上使用新 suite `ghost_control_revocation_repro_20261021` 重跑 6 项：
text/native × enabled、disabled_initially、disabled_after_first。初始关闭两项均不发送
hint；首轮后关闭两项均在第二次 HTTP 前明确拒绝，`revoked_hint_resent=false`；一直开启
两项保留 hint。脱敏指标见
[attempt-gate-revocation-20261021.json](../../tests/manual/ghost_memory_routing/reports/attempt-gate-revocation-20261021.json)。

复验经过真实 AppContext、scoped GhostInboxStore、task entry、kernel、ApiProvider 和
loopback HTTP；未调用 KoboldCpp，也没有模型收益、成本或延迟结论。

## 未完成或阻塞 / 下一步

本实验的原始负结果仍保留；当前 Attempt Gate 已闭合该 loopback 结构缺口。
后续仍需覆盖 codec/configure_request/compaction 之后的 payload、拒绝后的协议窗口和恢复语义。
不能只给 tool-results 加第二份 Ghost 校验，不能为过门而静默删历史，也不能声称服务端已经
看过的 hint 被撤回。需要覆盖 normal send、native result、repair、receipt、resume、compaction、
scope 和拒绝后的恢复。目前没有模型语言收益、成本/延迟收益或跨任务证据；World Model
仍保持 SHADOW。
