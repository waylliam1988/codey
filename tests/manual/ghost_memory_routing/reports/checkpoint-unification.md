# Canonical Checkpoint Unification

实验实现 `ec89d516983e8407ef9879be190f1cd6a9946559`；指标/严格复算/破坏证据测试
`0760ed08cfc8799af2e61e8afe686e51162e494f`。状态 **EXPERIMENT_ONLY / INCOMPLETE**。

## 目标与配置

确认单独删除 export 会不会丢失压缩后的早期事实，并测已有 durable context_edit 是否能
同时提供来源隔离和重启恢复。三个实验臂不改变生产源码：baseline、删除 export、
canonical 条目摘要→已有 admit_write/context_edit→删除 export。

- 模型 synthetic-source-lifetime；临时 loopback HTTP，非 live 模型，未调用 KoboldCpp。
- 两种 codec（Completions / Responses）、native done；reopen / AppContext restart；final on/off。
- 宽预算 context/reserve=65536/8192，窄预算=16384/2048；recent=1000，output=128，temperature=0，timeout=5s。
- counter 为 serialized characters/4 向上取整，标记 estimated，不冒充 tokenizer。
- 每 cell 五个 prelude + 一个 final StartTask，max_turns=1。只有第一任务含早期事实。
- 每个 prelude 有 210 行合成历史，done 不回显。真实压缩器的 summary fixture 仅复制 source 中存在的标记。
- 每臂/预算 8 cells，各一次；共 **48 cells、288 个任务、288 次主 HTTP + 160 次辅助 HTTP**。
- 576 项任务 provider effect 成功结算（send_turn 和本地 acknowledge），辅助 HTTP 尚未纳入此 effect 契约。
- canonical_edit 只读 durable 消息来源，额外摘要一次，将前四任务范围替换为 assistant 摘要，保留尾部。
  不持久化 Ghost-bearing live 摘要，不改工具调用/结果、Evidence、Permission 或 CompletionProof 事实。

## 结果

各行 facts 分母 4（两协议 × on/off），off hint 分母 2；所有 healthy on 均 2/2。

| 预算 | 实验臂 | reopen facts | restart facts | reopen off hint | restart off hint |
|---|---|---:|---:|---:|---:|
| 宽 | baseline | 4/4 | 4/4 | 2/2 | 0/2 |
| 宽 | 只删除 export | 4/4 | 4/4 | 0/2 | 0/2 |
| 宽 | canonical edit | 4/4 | 4/4 | 0/2 | 0/2 |
| 窄 | baseline | 4/4 | 0/4 | 2/2 | 0/2 |
| 窄 | 只删除 export | 0/4 | 0/4 | 0/2 | 0/2 |
| 窄 | canonical edit | 4/4 | 4/4 | 0/2 | 0/2 |

两协议结论一致。所有 cell 在生命周期切换前均实际产生并接受含早期事实的 checkpoint；
宽预算 bounded replay 仍含原始事实，窄预算已裁掉它。该前置条件由复算器强制检查。
canonical source 和提交的摘要均不含注入 hint；所有 durable entries 均无该 hint。
因此新证据否定“直接删除 exporter 已足够”，支持已有范围变换作为统一消费者。

## 复现

```powershell
foreach ($budget in @('full', 'trim')) {
  $mode = if ($budget -eq 'full') { 'full_replay' } else { 'trimmed_replay' }
  foreach ($tag in @('base', 'drop', 'edit')) {
    $arm = @{base='baseline'; drop='durable_replay'; edit='canonical_edit'}[$tag]
    python tests/manual/ghost_memory_routing/protocol_source_lifetime.py --suite "checkpoint_${budget}_${tag}_repro" --checkpoint $mode --handoff-source $arm --lifetime window_reopen app_restart --metrics-out "artifacts/ghost_memory_routing/checkpoint-${budget}-${tag}-metrics.json"
  }
}
python tests/manual/ghost_memory_routing/checkpoint_unification_report.py --reports artifacts/ghost_memory_routing
python tests/manual/ghost_memory_routing/checkpoint_unification_report.py
```

前三段可重新产生 48 cells；最后一条独立复算已提交的脱敏矩阵，无 raw state 或模型也可运行。
生成时 suite 后缀为 20261021；原始请求/回复、state、counter、压缩 diagnostics、manifest、console
仅在 ignored artifacts。setup 与先前 Protocol Source Lifetime 不并入上述分母。

## 限制与生产结论

生产未改默认 prompt，也没有合并新功能。候选使用既有 durable writer，但来源 head 比较在
实验代码中，不能当作原子 source fence；未证明并发、branch/delete、失败、idempotency。
该实验仍同时运行旧压缩和 canonical 摘要，宽预算每 cell 额外一次辅助请求；窄预算两类
消耗可能互相抵消。不能将此作为最终成本改善，也没有 live 模型延迟或安全结论。
warm consumer、native pending delivery、统一的辅助 effect settlement、来源预算和最终 gate 待实现。

相关回归 **122 passed (6.95s)**，包含两个报告完整性测试、durable/context/inbox/fork/CAS/
generation/provider authority、conversation resume 和真实压缩契约。完整命令见进度文件。
Ruff/diff 通过；本阶段生产源码未改，类型检查沿用上一阶段三工具结果，后续生产修改再跑。

下一阶段在隔离候选工作树实现 durable 来源绑定与原子提交，接真实消费者后再评估生产合并。
目标是单一 durable authority、可重建协议表示、Ghost 来源/撤回与唯一最终发送 gate；
不新增 Ghost 数据库、平行恢复历史、compatibility 或 fallback。World Model 保持 SHADOW。
