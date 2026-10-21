# Final Payload Admission Placement — offline report

- 日期：2026-10-21；负责人：Codex。
- 状态：EXPERIMENT_ONLY / INCOMPLETE；不合并候选 guard，0.6.1 / 0.6.2 未完成。
- 实验代码：`7453bc5a`；受测修正：`996605eaa16f58fba234a29b6b98d6c485d9d150`。
- 假设：最终准备后的 guard 仍早于 lease/retry；每个 physical attempt 才能覆盖这些时隙，
  但原文精确匹配不能自动证明 compaction source 的编码来源。

## 模型、任务与对照

synthetic-payload-placement，suite `final_payload_placement_20261021_fixed`，临时 loopback HTTP。
两种真实 API codec（Completions / Responses）；一个 done 任务，10 场景 × 3 位置 × 2 codec，
60 cells，各一次。正常、初始 off、lease expiry/revoke、503 retry expiry、configure_request
rewrite、ASCII compaction 正常/lease expiry、中文多行含引号正常/compaction。

候选在实验进程替换 kernel predicate，把同一个 workspace validator 移到 `_admit_request`
返回后或 `_generate_attempt` 入口；不叠加两份判定，不修改生产文件。辅助模型也受同一位置影响。
真实 builder、kernel、API codec、request lease/retry、compaction orchestration 和 HTTP；
history、响应、时间、counter、policy 是 fixture。原 history 的入库是已声明 fixture，
不冒充跨 run admission 已经实现。

context 65536、reserve 8192、keep recent 12000、output 128、temperature 0、timeout 5s、max turns 1。
counter 为显式 60000/40 pressure fixture（合法 method=estimated），不是 tokenizer 或真实容量测量。
在首次 lease 或 retry 前显式推进时间到 expiry 之后，或修改实际 scoped store 的 session control。
没有 scorer、语言效果评分、live 模型成本/延迟结论；token usage 未测量，不记为零。

## 实际结果

| 位置（每项 20 cells） | 实际 HTTP | 失效 source HTTP | rewrite HTTP | 健康对照被拒 |
|---|---:|---:|---:|---:|
| kernel 基线 | 28 | 10 | 2 | 0 |
| preparation 后 | 20 | 8 | 0 | 2 |
| lease 后 attempt | 12 | 0 | 0 | 2 |

共 60/60 cell 完整、60 次合成 HTTP。unsafe 按请求计数，不按 cell 冒充模型失败率。
两种 codec 的每一场景结论一致：

| 场景 | kernel | preparation | attempt |
|---|---|---|---|
| lease expiry / revoke | 仍发已失效 source | 仍发已失效 source | 拒绝，0 HTTP |
| retry expiry | 有效首发 503 后重发过期 source | 同样重发 | 保留首发，阻止重试 |
| wire rewrite | 发送被改写 source | 拒绝，0 HTTP | 拒绝，0 HTTP |
| compaction lease expiry | 辅助与主请求均发过期 source | 已向辅助发送，才拒绝主请求 | 辅助发送前拒绝 |
| 中文含引号 compaction | 正常完成 | 正常输入误拒 | 正常输入误拒 |

最后一行的拒绝原因均为 `ghost_directive:not_in_provider_payload`。辅助输入有原文的 JSON
转义表示，校验器只持原文精确匹配契约；不能从这个失败推出 hint 无效或降低安全门槛。
没有引入 unescape 重试、跳过辅助调用、缺 metadata 放行或降低 source budget 检查。

开启、初始 off、中文直接发送、ASCII compaction 四组 × 两种 codec，三个位置实际 payload
摘要序列均一致（8 组对照）。所有 acquire/release 平衡，拒绝没有泄漏 lease。
payload 摘要来自实际捕获的序列化请求，只用于本实验比较，不冒充生产 trace/ContextEpoch。

## 复现与异常归档

```powershell
python tests/manual/ghost_memory_routing/payload_admission_placement.py --suite final_payload_placement_repro --metrics-out artifacts/ghost_memory_routing/payload-placement-repro-metrics.json
python -m ruff check tests/manual/ghost_memory_routing/payload_admission_placement.py
```

metrics 见 [JSON](payload-placement-metrics.json)。新 suite 不覆盖旧目录，exit 0 只代表完整性。
首次 suite `final_payload_placement_20261021` 在首个 cell 因实验使用非法 count method 失败，
无完整 cell、无 HTTP。修正 fixture 跟随生产枚举后使用新 suite 完整运行；保留本地
failure、时间、命令与 stack，不算模型行为阴性，不重复计入样本。

原始请求/回复/state/manifest/policy/log 只在 ignored
`artifacts/ghost_memory_routing/final_payload_placement_20261021_fixed/`；无 KoboldCpp 调用。
生产默认 prompt 未改变；Ghost 不新增 Evidence、Permission、CompletionProof 或工具权限。

## 收益、限制与下一步

本轮确定 gate 必须晚于 lease，并且每次 retry 和辅助模型发送都要经过同一判定。
原样搬移因正常编码输入误拒而不足以生产合并，当前保持 EXPERIMENT_ONLY；没有生产回滚需求。
下一步验证显式 producer 表示契约和来源继承，尤其 history-only source 被摘要后无法再做原文字面匹配。
尚未证明跨 run/resume、并发 scope、repair/receipt、未知远端 web history 或完整 trace/wire parity。
World Model 继续 SHADOW。
