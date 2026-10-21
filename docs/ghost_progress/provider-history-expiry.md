# Ghost 实验：Provider History Expiry

- 实验名称：Provider History Expiry
- 状态：INCOMPLETE
- 负责人：Codex
- 开始 / 更新日期：2026-10-21
- 关联 Roadmap 验收门：0.6.1，生命周期前置
- 实验 commit：`bf7f1b8f`（探针、脱敏指标与负结果报告）；基线生产代码 `3d013af1`
- 生产合并结论：EXPERIMENT_ONLY

## 目标和假设

Scope Authority 仅验证新 prompt，尚不能证明后续发送的完整 provider history 受准入保护。
假设首次 hint 合法，第一次响应后到期，text/native 的第二次实际 HTTP request 仍携带历史 hint。
用显式模拟时间和固定响应复现，不调用 KoboldCpp，不把结构缺陷写成模型行为阴性。

## 实验配置

- model / suite / endpoint：`synthetic-history-expiry` / `provider_history_expiry_20261021_recorded` / 临时 loopback HTTP。
- tasks / cells / reps：一个 read→done 合成任务；text/native × expiring/not-expiring，对照四项各一次。
- on/off 对照：hint 始终存在；主要变量是第二次发送前是否跨过 expiry。
- 首次时间 2040-01-01T00:00:00Z，第二次时间 T00:00:02Z；expiring 的 expiry 为 T00:00:01Z。
- builder 为 planning_readonly；kernel policy 只有 project.read/control；read executor 返回固定合成结果。
- provider 固定两条响应，真实 ApiProvider HTTP，max turns 2；归档不覆盖旧目录。
- context 32768，reserve 8192，keep recent 12000，output 128，temperature 0.3，timeout 5s。
- 本轮只保留实验探针和负结果；不新增生产 validator、compat 或 fallback。

## 本次完成 / 验证

四项 probe 全部完成，两项有效期内对照均正确保留 hint；text/native 两项 expiry 都在第二次 HTTP
请求里重新发送过期 hint。不是模型行为测量，模型行为评分保持 null。
text 的第二轮调用了 validator，但其 `require_all=False` 且仅检查新 prompt，未阻止历史内容重发；
native 的 pending tool-results 路径跳过了该调用（admission clock 仅首次读取）。

```powershell
python tests/manual/ghost_memory_routing/history_expiry_probe.py --suite provider_history_expiry_20261021_recorded --metrics-out tests/manual/ghost_memory_routing/reports/history-expiry-metrics.json
python -m ruff check tests/manual/ghost_memory_routing/history_expiry_probe.py
```

结果：4/4 结构探针完整，2/2 expiry cell 暴露缺口；每项两次 HTTP 请求，共八次合成 HTTP，无 live 模型调用。
命令 exit 0 表示探针成功完成，不表示 admission gate 通过。新复现需使用新 suite，禁止覆盖原目录。
脚本支持未来的明确 expiry 拒绝，不要求生产继续发送旧 hint 来满足探针。
原始请求、回复、manifest 和结果只存本地 ignored archive；仓库仅保存
[脱敏指标](../../tests/manual/ghost_memory_routing/reports/history-expiry-metrics.json) 与
[报告](../../tests/manual/ghost_memory_routing/reports/history-expiry.md)。

## 当前生产边界

- 是否经过真实 provider-send：是，ApiProvider → loopback HTTP；无 live 模型。
- 是否改变默认 prompt：否。
- 支持模型 / 任务 / 配置：固定响应、两个协议的 expiry 边界，不作模型推广。
- 生产合并结论：EXPERIMENT_ONLY。

## 未完成或阻塞 / 下一步

当前缺口阻止完整 0.6.1 验收，但不否定已独立验证的 scope 安全修复。下一实验需要确定完整
payload/history 的单一准入宿主，包括 tool-result、repair、native receipt、resume、compaction 和禁用。
ApiProvider 的 `_admit_request` 可见 codec 构建和 compaction 后的真实 payload；现有 request policy
只接收 target/deadline，不接收 payload，不能直接把它当作 Ghost admission 已完成。
网页 provider 的远端 history 可见性也必须明确，不能伪称看到了完整历史。
先验证失败关闭及恢复语义，不为消除红灯而丢弃历史、添加第二个 Ghost validator 或插入重复 callback。
本次不合并 history 生产实现；World Model 继续 SHADOW。
