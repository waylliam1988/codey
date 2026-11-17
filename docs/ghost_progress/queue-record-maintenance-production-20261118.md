# Queue 逐记录维护与 checkpoint 准入

- 日期：2026-11-18；关联0.6.2/0.6.4；状态：`PRODUCTION_MERGED`，与本记录同一master生产提交。
- 冻结：`1b8d3e3d`；复用[四领域基线](maintenance-record-scope-20261118.md)中queue的6正/6负证据。
- 无模型调用：修复控制与持久记录契约，不新增模型收益或跨模型推广声明。

## 默认生产行为

普通App/default Sleep daemon既有queue回收入口及直接`reconcile_stale_claims`默认生效，无新flag。
queue在自身写锁内读取实际inbox的不可变控制快照，完整校验其他scope的entries；不缓存权限。
只对具备维护资格的过期running lease追加原有严格transition，保留allowed邻居实际回收。
replay仍消费实际事件，不读取当前控制，因此关闭期间没回收的租约不会因当前重新开启而在
重建时偷偷被回收；下一次真实维护才能回收。其他三个独立控制不影响已接受queue状态的维护。

queue project使用raw normalized ref，session使用session_key。形似hash的原始session控制仍
按原始身份编码，不与另一个session的已编码ref混同。user deny继承至全部记录；project/session
deny保护自己的记录。现有session记录没有canonical父project来源，任一project deny存在时，
session租约保守暂停回收；user及其他允许project仍可回收，不猜父子关系。

存在任何maintenance限制时，queue的内部自动checkpoint和显式`compact_if_needed`暂缓
全库checkpoint，保留消费历史，继续追加allowed transition；恢复允许后可正常checkpoint。
这不是选择性compaction：消费窗口可能超过原预算，长期禁用下的窗口有界化仍待解决；物理
历史retention/purge也未闭合。显式删除仍由现有删除权威控制，不受维护deny阻止。

## 验证与失败归属

- 新增46项全通过：普通App目标project/session × 触发user/project/session × on/off **12/12**；
  另34项覆盖user/legacy、其他控制、hash原始身份、未知父project、其他scope损坏/读取故障、
  满200条容量、未来/过期lease、重试上限、恢复控制、删除、冷重启/rebuild、真实写故障回滚、
  真正超预算的checkpoint暂停/恢复，以及deny先提交/维护先持锁两种线程顺序。
- 最终扩大20文件 **495 passed**，79.18秒，无跳过/失败；含全部46项及既有Sleep/控制/queue/
  源转换/transition/ownership回归。未跑全量pytest。
- 原四领域scorer完全不改，重验 **6 passed / 6 failed**。所有12项的queue子比较通过；
  六个关闭组仍在Hebbian/Affinity/continuity变化，三个领域的失败保留，不能称总门通过。
- 全仓Ruff、完整702生产文件mypy/Pyrefly/ty、precision gate通过。三个owned文件三检查器
  通过，额外`--disallow-any-expr` owned零诊断；未修改依赖10864项/334文件导致该额外审计
  exit1，独立记录。全树Pyrefly158既有warning/35 suppressed，owned三个既有转换warning，
  未加ignore/cast/豁免或改检查范围。

首候选12个App打印成功后，直接owner路径暴露写事务内新建控制reader导致SQLite初始化
锁竞争；中断，保留日志/state，不算完成批次或模型负结果。现reader在mutation前构造，
每次实际设置在queue写锁内重新读取。第二批44通过/2夹具错误：公开listing按契约隐藏
过期持久项；改用实际行验证种入，另保留过期reader断言，再46全通过。
hash原始session fixture也按queue真实编码校正；不改scorer。额外Any首检的一处inline
指标dict推断已用具体类型修正。打印12格指标来自注解修正前46批，最终495批以最终
三个owned源字节执行相同46断言；不把两批伪写成一次运行。最终702生产文件hash检查无变化。

```powershell
python -m pytest tests/test_ghost_queue_record_maintenance.py -q -s --tb=short
python -m pytest tests/test_ghost_maintenance_record_scope.py -q -s --tb=short
```

第二条仍预期记录6个其他领域生产失败。完整扩大命令、逐项静态命令与脱敏指标见
[生产报告](../../tests/manual/ghost_memory_routing/reports/queue-record-maintenance-production-20261118.json)。
原始log/XML/SQLite/hash在`.e2e-artifacts/ghost-queue-record-maintenance-20261118/`，不入仓。

回滚：revert本生产提交并在空闲时重启，保留SQLite与显式控制。没有schema/event格式变更，
旧reader仍可读原transition。回滚会恢复错误的全库回收行为，按现有maintenance控制隔离。

下一步继续Hebbian/Affinity/continuity的逐记录维护、跨边端点与容量/replay语义，再补有界
选择性checkpoint、旧非canonical/超长session身份来源、pending交接/硬退出、混版本与
retention。完整0.6.2/0.6.4/G0未完成；World Model继续目标，新候选须提供新增预测信息，
不重复已终止的重复事实候选。用户原改动保留，未创建worktree。
