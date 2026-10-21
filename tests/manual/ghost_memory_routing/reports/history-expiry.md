# Provider History Expiry：负结果

状态：EXPERIMENT_ONLY；完整 admission 验收仍 INCOMPLETE。没有 history 生产修复。

在 `3d013af1` 上，使用实际 planning builder、shared kernel 和 ApiProvider，固定 HTTP 响应
产生 read→done 两轮。显式模拟时间在第一条 HTTP 请求之后从 2040-01-01T00:00:00Z
推进到 T00:00:02Z，expiry 为 T00:00:01Z。

| 协议 | 对照 | 第二次 payload 带 hint | admission 时间读取次数 | 结论 |
|---|---|---|---:|---|
| text | 不过期 | 是 | 2 | 对照完整 |
| text | 过期 | 是 | 2 | 过期历史重发 |
| native | 不过期 | 是 | 1 | 对照完整 |
| native | 过期 | 是 | 1 | 工具结果发送绕过当前 gate |

四项各一次，均 done 且各两次 HTTP 请求。无缺失或异常 cell；每项超时 5 秒。
这是确定性的结构故障，不是统计性的模型行为阴性。没有使用 KoboldCpp，没有语言评分。
完整配置和脱敏数据见 [history-expiry-metrics.json](history-expiry-metrics.json)。

```powershell
python tests/manual/ghost_memory_routing/history_expiry_probe.py --suite history_expiry_repro --metrics-out artifacts/ghost_memory_routing/history-expiry-repro-metrics.json
```

同一 suite 已存在时拒绝覆盖；首次记录的 suite 是 `provider_history_expiry_20261021_recorded`，
原始 request、result、manifest 留在该本地 archive，未提交仓库。脚本的成功退出表示四项探针
完成，不表示生产安全通过；未来明确拒绝 expiry 的路径也可记录，不强迫生产满足旧的不安全行为。

根因：kernel 检查的只是当前新 prompt。ApiProvider 拥有历史，text 后续校验的 rejected 结果
没有阻止旧 messages 重发，native tool-result 路径则在 kernel 中直接跳过 admission。
初次 scope gate 与 trace parity 不能替代完整 payload/history 的寿命检查。

下一步应确定 codec/compaction 后的单一准入宿主以及持久恢复所需的来源绑定，覆盖普通 generation、
tool-results、protocol repair、native receipt、resume 和禁用；不能通过重复校验、静默丢弃
历史消息或扩大 Ghost 权限解决。网页 provider 的历史不可见性需要单独说明。
实验代码保留；默认 prompt、Evidence、Permission、CompletionProof 均未改变。
