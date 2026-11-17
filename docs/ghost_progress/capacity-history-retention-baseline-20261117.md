# Ghost 容量压力下的历史保留基线

- 日期：2026-11-17；冻结生产基线：`7244c1e3`。
- 状态：`EXPERIMENT_COMPLETE`（有效基础设施负结果）。
- 模型、endpoint：不使用；不恢复已停止的语言批次。
- 前置：[低容量普通删除已默认修复](scope-history-retention-production-20261117.md)。

## 冻结目标、变量与停止条件

假设：已经完成的低容量普通删除修复，在现有容量阈值之上仍能保留原领域历史；默认Sleep
维护与新observation写入也不应把未显式purge的原始历史当作派生缓存抹去。
主要变量是历史事件压力（低于阈值/超过真实count阈值），不修改生产阈值或scorer。

六个领域（inbox、Hebbian、continuity、work queue、affinity、Sleep）× 三scope × 两种压力
× 两个普通消费者（默认Manifest scope删除、现有Sleep.run_once），共72项。
另有observation × 三scope × 两种压力的新完成回合写入，共6项，总计78项确定性cell，
每cell执行一次；不把离线cell当作模型行为样本。

事件压力用既有store生成的合法事件作幂等upsert/report重放，每个envelope给独立event_id；
原领域payload不改、投影不增加虚假新来源、consumer cursor同事务确认。超过count阈值的
单元可能同时超过bytes阈值；本轮不声称分别隔离两个阈值。低压力对照只追加两个事件。
observation容量用真实schema的独立run事实准备，然后从公开append_completed写入新回合。
不绑定应用runtime，不伪造canonical receipt，不修改用户state。

每个cell先证明seeded来源当前可消费、历史非空、真实count压力满足条件；之后从上述
普通消费者执行，核对操作成功、删除后不可消费（或维护/append后仍可消费）、重开/既有
reducer重建后的可见性，最后比较原SQL sequence、event_id和payload前缀完整保留。
主要指标为历史改写cell数，目标0；次要指标为Manifest完成、正常消费者状态和rebuild成功。
只有到达最后历史断言才算有效历史负结果；准备、服务或前置行为错误独立记录，不混入。

出现前置错误先修复fixture并保留无效批次；发现真实生产失败保留证据，不跳过、改阈值或
放宽断言。完成本矩阵后停止采样，分别记录生产/测试归属，再决定最小生产修复。
完整物理purge、reset、count/bytes独立边界和不同操作并发不由本矩阵证明。

## 命令

```powershell
python -m pytest tests/test_ghost_capacity_history_retention.py -q --tb=short --junitxml=.e2e-artifacts/ghost-capacity-history-20261117/baseline.xml
```

## 结果与归属

**78项全部有效，48 passed / 30 failed**；39个低压力对照全部通过。超过阈值时：

- 普通Manifest删除：inbox、Hebbian、Sleep的9项仍通过；continuity、work queue、affinity
  各三scope共9项丢失历史，来自删除事务内自动compaction。
- 默认Sleep维护：六领域各三scope共18项丢失历史。
- observation新回合追加：三scope共3项物理裁剪历史事实。

所有失败均位于最后的原始历史前缀比较；它们的操作成功、消费者可见性和本次重建检查
先通过。不声称出现删除来源复活，也不将压缩成功但丢审计历史称为完整retention通过。
无fixture准备错误、缺失cell、模型或服务失败。

[脱敏逐cell报告](../../tests/manual/ghost_memory_routing/reports/capacity-history-retention-baseline-20261117.json)
不包含原始事件正文、临时路径或state。完整日志、JUnit及失败state留本地
`.e2e-artifacts/ghost-capacity-history-20261117/`及本次pytest保留目录。

既有相关回归 **245 passed / 2 subtests passed**：

```powershell
python -m pytest tests/test_ghost_scope_history_retention.py tests/test_ghost_graph_sqlite.py tests/test_ghost_sleep.py tests/test_experience_memory.py tests/test_ghost_continuity.py tests/test_ghost_work_queue.py tests/test_ghost_affinity.py -q --tb=short --junitxml=.e2e-artifacts/ghost-capacity-history-20261117/baseline-related.xml
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
```

全部依次通过；mypy覆盖701生产模块，Pyrefly为0 errors / 35既有suppressed / 158 warnings。
新测试另经mypy、Pyrefly、ty通过。额外mypy --disallow-any-expr在本文件零诊断；未修改依赖
187文件4065项诊断使扩展命令exit1，不记为全仓Any审计通过。未放宽配置、引入cast、ignore
或豁免。未运行全量pytest，也没有重跑已停止的语言批次。

## 下一步

普通消费者需要保留同一领域日志的原始事件，同时以有界checkpoint/重放窗口维持原有
工作集容量；不能简单关闭compaction，也不能另造全局权威日志。需要核对schema升级、
快照和窗口原子性、重复维护、清空快照、删除/rebuild及硬退出恢复；observation事实与
有界可检索窗口需分离。reset、物理purge及完整0.6.3仍未由本实验验证。
