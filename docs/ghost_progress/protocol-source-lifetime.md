# Ghost 实验：Protocol Source Lifetime

- 实验名称：Protocol Source Lifetime
- 状态：INCOMPLETE
- 负责人：Codex
- 日期：2026-10-21
- 实验 commit：本文件随实验脚本提交；完整 hash 在结果记录中补充。
- 关联 Roadmap 验收门：0.6.1 / history 生命周期与撤回
- 生产合并结论：EXPERIMENT_ONLY

## 目标和假设

先前提出复用 ContextLedger 磁盘 journal 保存来源的建议需要更正：生产 ApiProvider 初始化和
bind_context 均构造无 root 的内存 ContextLedger，两个磁盘工厂目前只有测试消费者。
真实 restart authority 是 DurableSession SQLite。不能将 Ghost 候选另存一套历史，
也不能先将候选作为 canonical conversation fact 持久化。

本实验假设旧 hint 主要滞留于 warm protocol view 和 export_work_state → handoff，
而非原始 durable user message。通过真实 task submission / plan / activation / builder /
admission / recorded provider / HTTP / settlement 比较四种生命周期。

## 实验配置

- 模型 / suite / endpoint：synthetic-source-lifetime / 待运行正式矩阵 / 临时 loopback HTTP。
- Completions、Responses 两种 codec；native tools；四种 lifecycle × on/revoke，共 16 cells。
- 每 cell 两个独立 StartTask，各一次 HTTP，max_turns=1；reps=1。
- 首任务开启，第二任务保持开启或真实 session-scoped admit_hints=False。
- warm：相同 provider、相同 window；window_reopen：生产 registry 显式失效后由真实 planner 重开；
  fresh_provider：保留 AppContext/SQLite、更换空 provider；app_restart：close/reopen AppContext 和 provider。
- window_reopen 不等于 token-pressure rollover，不据此声称后者通过。
- context 65536 / reserve 8192 / keep_recent 12000 / output 128 / temperature 0 / timeout 5s。
- 固定 synthetic directive producer、非回显原生 done；不 patch gate、replay、window 决策或 settlement。
- 注入 connect_provider 使用既有服务依赖入口，避免 synthetic 模型触发真实模型发现。

## 本次完成

建立 `tests/manual/ghost_memory_routing/protocol_source_lifetime.py`，捕获每次真实规划的
window action、handoff/replay 是否含 hint，并读取 durable entries/context、conversation snapshot、
transport/export 和实际 effect settlement。只导出布尔、计数和摘要；raw 请求/事件/state 本地归档。

## 验证命令和结果

正式矩阵、扩展回归和完整 commit 待下一次记录补充。

setup 第一、二次因实验断言错误停止：provider_send 属于 effect_category，而非 tool_name；
成功原生 done 同时结算 send_turn 和本地 acknowledge_tool_results，后者不发 HTTP。
生产未为脚本增加兼容；失败归档为 source_lifetime_setup_20261021、source_lifetime_setup2_20261021。
第三次 setup warm on/revoke 两项完整，2 tasks/cell；revoked hint 第二次仍发送，durable 无该 hint。
setup 不是正式矩阵样本，不混合计数。

```powershell
python tests/manual/ghost_memory_routing/protocol_source_lifetime.py --suite source_lifetime_repro --metrics-out artifacts/ghost_memory_routing/source-lifetime-repro-metrics.json
python -m ruff check tests/manual/ghost_memory_routing/protocol_source_lifetime.py
```

## 当前生产边界

- 是否经过真实 provider-send：是，实际 ApiProvider/HTTP；确定性 loopback，不是 live 模型。
- 是否改变默认 prompt：否，只有实验 synthetic producer，生产源码不改。
- 支持模型 / 任务 / 配置：限定上述确定性 native done、两任务生命周期；无 language 或模型收益结论。
- 生产合并结论：EXPERIMENT_ONLY；World Model 保持 SHADOW。

## 未完成或阻塞

正式矩阵尚待运行；不证明 compaction 派生、token-pressure rollover、native 未完成工具恢复、
远端 web history 或模型回显后的来源传播。无模型成本/延迟测量、无生产候选晋级。

## 下一步

完成 16 cell 矩阵，扩大相关 conversation/provider/durable/Ghost pytest 范围，记录负结果并提交。
根据真实来源归属选择 metadata 宿主，避免新建 persistence root 或重复 admission。
