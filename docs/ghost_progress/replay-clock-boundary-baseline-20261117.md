# Ghost 固定日志重建的隐含时间基线

- 日期：2026-11-17；冻结生产基线：`d1a40185`。
- 关联：0.6.4 确定性 replay 与显式时间。
- 状态：`EXPERIMENT_COMPLETE`（有效基础设施负结果）；生产结论：`EXPERIMENT_ONLY`。
- 模型/endpoint：不使用。这是日志重建与故障隔离实验，不恢复已停止语言批次。

## 冻结目标与矩阵

假设：同一份持久 Affinity 事件在不同当前时间重建，得到相同节点、边、权重、状态、
顺序及投影 digest；损坏日志也可以明确拒绝，但不能用当前时间猜测历史时间。
拒绝必须原子保留上一次有效投影与原始事件，正常 reader 仍可使用该投影。

三个 scope（user/project/session）× 四种固定事件序列，共 12 cell，每项一次，
每 cell 先后使用 2026-01-01 与 2027-01-01 两个 UTC 时钟。另一个用例校验时钟夹具。
固定初始事件为两个研究概念节点及一条关联边；后续强化与衰减分别覆盖：

1. valid：合法时间戳正对照，必须实际改变权重并正常完成重建。
2. reinforcement：第二条不同来源强化事件的 ts 损坏。
3. decay：衰减事件 ts 损坏。
4. snapshot_basis：快照节点 last_decayed_at 损坏，随后在合法显式事件时间衰减。

日志通过真实 GhostGraph 事务保存；故障注入时保留初始有效投影，并确认 consumer cursor
与事件尾一致，随后调用真实 GhostAffinityStore.rebuild_from_events、重开 owner、公开
list_nodes/list_edges。只替换共享时间解析器使用的 datetime，既不改 transition，也不替换
store/rebuild/SQLite；合法序列是对该替换的负对照。随机 event_id 每 cell 只生成一次，
两次重建完全复用同一日志。比较实际节点/边 payload 的 SHA-256，不比较数据库文件、
随机操作标识或导出审计时间。不能将该故障注入描述为普通 producer 已产生坏时间。

主要指标：两个时钟下结果不一致的 cell 数，目标 0；准备、历史保留、先前投影可用性或
回滚错误单列为基础设施/生产失败，不能混入确定性指标。达到预定 12 cell 后停止，不增 reps、
不改 scorer。生产 Python 全程冻结并比较哈希。合法日志失败先调查夹具/生产归属。

不验证完整 Sleep 逻辑时间、其他领域 replay、真实模型发送、跨机器或 World Model 完成。

```powershell
python -m pytest tests/test_ghost_replay_clock_boundary.py -q --tb=short -o junit_logging=all --junitxml=.e2e-artifacts/ghost-replay-clock-20261117/baseline.xml
```

## 结果

最终 12 cell 全有效：3 个合法时间正对照通过，9 个损坏时间场景全部在最终确定性比较失败；
另一个时钟夹具用例通过，所以 pytest 为 **4 passed / 9 failed**。三种损坏均跨三个 scope
复现。rebuild、重开 owner、公开 reader、原始事件保留断言全部通过后才进入比较。

| 固定序列 | user | project | session |
|---|---|---|---|
| 合法时间 | 通过 | 通过 | 通过 |
| 强化 ts 损坏 | 权重不同 | 权重不同 | 权重不同 |
| 衰减 ts 损坏 | 权重/状态不同 | 权重/状态不同 | 权重/状态不同 |
| 快照衰减基点损坏 | 权重不同 | 权重不同 | 权重不同 |

例如 user 的衰减 ts 损坏时，相同 alpha 节点在两个时钟下分别为 0.352/active 与
0.021169/expired，关联边也从 0.128 变成 0.015545。快照坏基点让相同节点分别为
0.088 与 0.176。差异不仅是导出审计时间，而是实际生产投影和 reader 的状态/排序输入。

归属：Affinity event 校验只要求 ts 非空，共享 graph_primitives.parse_ts 将坏时间转换成
datetime.now；强化、衰减和快照后的衰减因此消费了隐含墙上时间。生产违反固定日志重放
契约；不能用改变系统时间、放宽 digest 或忽略权重差异修测试。后续修复需拒绝坏时间，
保留合法历史格式及原子回滚，不凭空猜测过去，也不能把坏数据伪装成 no_change。

首轮和第二轮分别为 1/12、2/11，通过/失败数量不作为有效矩阵：夹具最初使用未编码的
project/session 引用，导致公开 reader 无匹配；首轮还误把 alpha 强化后再减半恰好恢复
原权重当成无转移。已改为生产 _scope_ref 编码，并检查真实发生衰减的边；没有更改生产、
时钟、事件时间、损坏变量、预定矩阵、阈值或最终确定性断言。三次 JUnit/日志均保留，
最终批次不与之前相加。生产 702 个 Python 文件在全部运行中哈希不变。

[脱敏逐 cell 报告](../../tests/manual/ghost_memory_routing/reports/replay-clock-boundary-baseline-20261117.json)。
完整日志、JUnit 与数据库只留本地 `.e2e-artifacts/ghost-replay-clock-20261117/` 及 pytest
保留目录。不证明正常 producer 产生坏时间，不代表完整 0.6.4 或 World Model 已完成。

## 相关回归与类型检查

现有相关回归 **113 passed**，包括 graph primitives、Affinity 单转移、真实 SQLite 图、
损坏契约、Sleep 和 Hebbian。未运行全量 pytest，也没有模型请求。

新增测试的时间子类最初返回 datetime 而不是 Self，三个检查器均拒绝；额外 Any 审计还
发现 JSON 测量字典被推断为 dict[Any, Any]。已分别返回真正的子类实例、显式声明测量
字段类型；没有 casts、ignore、豁免或弱化检查。修正后矩阵结果仍为 4 passed / 9 failed，
最终检查以本地 baseline-verified-* 日志及脱敏报告为准。

最终串行全树 Ruff、mypy（702模块）、Pyrefly、ty 和 precision 均通过；Pyrefly 为
0 errors、35项既有suppressed和158 warnings。新增测试另经三种检查器通过，额外
Any审计 owned 零诊断；未修改依赖诊断仍使该扩展命令 exit 1，不能称全树 Any 通过。

```powershell
python -m pytest tests/test_ghost_graph_primitives.py tests/test_ghost_affinity.py tests/test_affinity_replay_single_transition.py tests/test_ghost_graph_sqlite.py tests/test_ghost_corruption_contract.py tests/test_ghost_sleep.py tests/test_ghost_hebbian.py -q --tb=short
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
python -m mypy tests/test_ghost_replay_clock_boundary.py --python-version 3.11
pyrefly check tests/test_ghost_replay_clock_boundary.py --python-version 3.11
ty check tests/test_ghost_replay_clock_boundary.py --python-version 3.11
python -m mypy tests/test_ghost_replay_clock_boundary.py --python-version 3.11 --disallow-any-expr
```
