# Ghost 混版本 warm owner 的历史保留基线

- 日期：2026-11-17；冻结当前生产基线：`da4cbfd8`。
- 历史producer：`7244c1e3`，真实schema7代码，通过Git对象导入，不复制worktree。
- 状态：`EXPERIMENT_COMPLETE`（有效基础设施负结果）；关联0.6.3。
- 模型/endpoint不使用，不恢复语言实机批次。
- 后续：[原事件物理删除防护](history-delete-guards-production-20261117.md)已在默认owner构造
  安装，将原42项全部修复为通过；本文件保留14/28原始结果，不代表旧reader或整个混版本runtime已隔离。

## 冻结目标、矩阵与停止条件

假设：当前schema8的事件保留窗口，不会被升级前已经实例化的schema7 owner再次物理重写。
通过全新Python子进程，在Git import finder下加载历史Ghost实现并创建真实schema7数据库、
七领域合法来源与长期持有的owner；准备阶段证明schema=7、来源可见、原始历史非空。
父进程用当前生产代码升级同一隔离数据库，再释放仍在等待的旧owner执行实际原存储方法。
没有降级PRAGMA伪造旧owner，没有复制checkout，没有修改旧方法或scorer。

七store × compact/reset两种旧操作 × schema7对照/当前升级并checkpoint/升级后普通reset，
共42项确定性cell，每项一次。14项schema7对照要求旧操作仍正常完成；28项升级处理要求
原始SQL历史前缀保留、当前消费者健康且可见性符合处理阶段。旧操作可明确拒绝；不能先
破坏历史再报告拒绝。旧错误、来源准备和进程故障分开，只有进入最终历史比较才记有效负结果。

record/graph的compact使用历史真实append(replace=True)与同事务projection/cursor确认写口；
observation使用历史公开append_completed在真实5000容量以上执行原自动裁剪。reset调用
各原owner的reset。当前checkpoint复用现有窗口写口，升级后reset使用普通控制面Manifest。
这验证领域存储边界，不声称旧版整个AppContext、真实provider发送或旧模型已被隔离。

主要变量为旧owner释放前是否发生升级与当前窗口推进；主要指标为升级后历史改写数，目标0。
另检查旧schema正对照、操作结果及当前消费者健康。完成42项后停止采样；保留无效准备与
真实负结果，依据影响决定最小生产修复，不改阈值、不跳过或放宽断言。
本矩阵不证明旧reader、已在执行的跨升级SQL事务、任意原始SQL管理者或完整混版本生命周期。

```powershell
python -m pytest tests/test_ghost_mixed_schema_history.py -q --tb=short --junitxml=.e2e-artifacts/ghost-mixed-schema-20261117/baseline.xml
```

## 结果

**42项全部有效，14 passed / 28 failed**。14项schema7正对照的原旧操作都正常完成；
28项升级处理全部通过准备与进程协议，且旧操作成功后在最后历史前缀比较失败。
旧record/graph replace/reset与observation trim/reset都能在旧owner未重建的情况下物理删除
新runtime保留的原始事件。没有把进程/准备故障算作历史负结果。

归属为生产事件表缺少物理删除约束，旧代码的构造期schema拒绝不足以保护已创建owner。
最小候选是在三张原始领域事件表上拒绝物理DELETE，保留当前窗口、投影和缓存的正常维护。
该候选只解决原日志物理删除，不能据此宣布旧reader或完整混版本runtime已隔离。

[脱敏逐cell报告](../../tests/manual/ghost_memory_routing/reports/mixed-schema-history-baseline-20261117.json)
保留14/28原结果，完整日志、JUnit及失败state只留本地
`.e2e-artifacts/ghost-mixed-schema-20261117/`与pytest保留目录。
相关回归 **200 passed**，无失败、错误或skip：

```powershell
python -m pytest tests/test_ghost_event_windows.py tests/test_ghost_reset_history_retention.py tests/test_ghost_reset_boundaries.py tests/test_ghost_corruption_contract.py tests/test_ghost_graph_sqlite.py tests/test_ghost_sqlite_store.py -q --tb=short --junitxml=.e2e-artifacts/ghost-mixed-schema-20261117/baseline-related.xml
```

首轮全树静态门及两新文件三种类型检查均通过，但额外Any审计发现owned的5项诊断：四处
json.dumps字典缺具体上下文类型、一处Popen.returncode动态表达式。已改为明确的通信payload
函数和wait返回的实际退出码，并在JSON边界收窄outcome；不改变历史代码、操作或历史断言。
完整42项类型清理后复验仍为 **14 passed / 28 failed**，逐cell确认没有准备或协议错误，
所有28项仍在同一个最终历史断言失败。随后串行全树Ruff、mypy（702个生产模块）、Pyrefly、
ty与precision全部通过；Pyrefly为0 errors、35项既有suppressed、158 warnings。两个owned
文件另经三种类型检查通过，额外Any审计owned零诊断；未修改依赖188文件4066项诊断使扩展
命令exit1，不能称为全树Any审计通过。首轮5项owned问题及修复记录保留，不新增cast、ignore
或豁免。日志为baseline-final-{ruff,mypy,pyrefly,ty,precision,owned-mypy,owned-pyrefly,
owned-ty,owned-any}.txt，类型清理复验为baseline-typed.xml/.txt。

未运行全量pytest、未调用模型。生产修复独立提交，不把本次负结果写成晋级。
