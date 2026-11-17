# Ghost 普通删除保留领域历史的生产修复

- 日期：2026-11-17；负结果提交：`41393284`，生产比较基线：`f09eddd2`。
- 状态：`PRODUCTION_MERGED`（本记录随独立生产修复提交到本地master）。
- 前置：[63项历史保留基线](scope-history-retention-baseline-20261117.md)。
- 范围：低于当前compaction阈值的普通user/project/session删除；无模型或HTTP调用，
  不恢复已停止的语言批次，不新增模型收益主张。

## 改动合同

inbox与Hebbian的scope delete追加领域删除事件，同事务更新当前projection；不把原始
事件重写成剩余快照。既有reducer按事件顺序应用scope删除，之后的新候选/新同槽偏好可用，
删除前的旧记录不回到projection。持久候选/run退役与旧writer fence继续生效。

Sleep保留原report事件并追加scope删除。删除计数和fallback从已应用删除事件的live报告
集合计算；删除B时不能把此前已删除的A恢复为latest。旧report仍可供显式历史审计，
普通latest不包含它。

observation保留原始事实行，以既有永久退役身份过滤普通read_all、read_committed与retrieve。
重复删除只计仍live的行；输入身份/退役登记保持同一事务。派生向量不是原始事件，应在相同
删除事务清除目标scope缓存；embedding计算期间的删除也需要在索引和排序阶段复查。

本次不改reset_all、拒绝单个candidate或独立容量compaction策略，不创建新schema或
长期默认关闭的flag，不生成额外receipt。完整容量retention及历史物理purge策略继续未完成。

## 验证进展与失败归属

原63项矩阵修复后全部通过。加入重复删除、新输入、Sleep连续删除及坏scope控制事件后，
连同既有相关测试 **149 passed / 133 subtests passed**。

首轮候选误改了Hebbian的remove_candidate而非scope delete，相关回归指出后已恢复该方法、
只修scope delete。两条旧inbox/Hebbian测试要求scope删除抹去事件正文，与Roadmap当前
合同冲突；改为同时断言消费者不可见、原始事件前缀保留及重建后仍不复活，保留其他scope。
两个修改测试文件的原有Any同步清除，不通过修改生产迁就旧断言。

新增“删除后写fresh-run”检查最初按run_id排序比较observation历史，把正常插入误判为
前缀改写；改为真实sequence顺序，原基线每cell只有一条observation，不影响原负结果。

首批扩大回归 **831 passed / 19 failed / 141 subtests passed**。其中1项暴露候选实现
遗漏派生向量清理，生产修复在同事务删除目标scope缓存，并在embedding写入/排序时复查
退役身份与原始receipt；新增index/query两个计算期间删除窗口与其他scope缓存保留检查。
其余18项来自原子性测试把“不可消费”表示为“原SQL行物理不存在”，已改为未退役行计数。
原有全表事务回滚快照、同ID不重做、fresh输入保留、双进程和三窗口硬退出断言完整保留，
未放宽并发/恢复门槛。三文件复验 **151 passed**。

追加式控制还发现同秒重复删除缺口：scope/计数/ts相同的控制没有独立event_id，内容hash
去重吞掉第二次删除，新输入再次删除后reducer不能正确重放。固定逻辑时钟、连续三次
delete→fresh input→delete，对inbox/Hebbian/Sleep各三scope共9项全部失败。退役fence
仍拒绝旧记录写回，不能把重建拒绝说成越过fence后已消费复活。每次领域删除需要独立事件
身份；Manifest仍原子确认并跳过completed，独立ID不表示重复执行同一已完成操作。

独立删除事件ID修复后，9项同秒测试全部通过。原63项矩阵已逐cell从最终JUnit核对：
**63/63通过**，其中21个不删除对照、42个删除/重开或重放cell。新增23项边界检查后，
本文件共86项验收。[脱敏报告](../../tests/manual/ghost_memory_routing/reports/scope-history-retention-production-20261117.json)
保留逐cell结果，不提交原始事件、临时目录或state。

## 最终验证

独立ID修改前扩大回归 **853 passed / 141 subtests passed**；最终源码对删除、重放、并发、
硬退出恢复及实际消费者的相关复验 **454 passed / 133 subtests passed**。这是两个批次，
不能相加或把前一批称为包含最后9项的862项最终扩大回归。最终命令：

```powershell
python -m pytest tests/test_ghost_scope_history_retention.py tests/test_ghost_inbox.py tests/test_ghost_hebbian.py tests/test_ghost_sleep.py tests/test_experience_memory.py tests/test_ghost_vectors.py tests/test_ghost_manifest_atomic_mutation.py tests/test_ghost_manifest_production_wiring.py tests/test_ghost_deletion_manifest_recovery.py tests/test_ghost_deletion_manifest_multiprocess.py tests/test_ghost_deleted_input_fence.py tests/test_ghost_deleted_source_lifetime.py tests/test_ghost_canonical_input_receipts.py tests/test_ghost_legacy_projection_admission.py tests/durable/test_operation_driver_restart.py tests/durable/test_legacy_chat_rescue.py -q --tb=short --junitxml=.e2e-artifacts/ghost-scope-history-20261117/production-final-recheck.xml
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
```

全部依次通过。mypy覆盖701个生产模块；Pyrefly为0 errors、35项既有suppressed、158 warnings；
不把通过说成全仓无动态类型。四个修改测试另经mypy、Pyrefly、ty检查全部通过：

```powershell
python -m mypy tests/test_ghost_scope_history_retention.py tests/test_ghost_inbox.py tests/test_ghost_hebbian.py tests/test_ghost_manifest_atomic_mutation.py --python-version 3.11
pyrefly check tests/test_ghost_scope_history_retention.py tests/test_ghost_inbox.py tests/test_ghost_hebbian.py tests/test_ghost_manifest_atomic_mutation.py --python-version 3.11
ty check tests/test_ghost_scope_history_retention.py tests/test_ghost_inbox.py tests/test_ghost_hebbian.py tests/test_ghost_manifest_atomic_mutation.py --python-version 3.11
python -m mypy codey/ghost/inbox.py codey/ghost/hebbian.py codey/ghost/observations.py codey/ghost/sleep.py codey/ghost/vectors.py tests/test_ghost_scope_history_retention.py tests/test_ghost_inbox.py tests/test_ghost_hebbian.py tests/test_ghost_manifest_atomic_mutation.py --python-version 3.11 --disallow-any-expr
```

最后的额外Any审计在九个owned文件中零诊断；未修改依赖377文件14037项诊断使该扩展命令
exit1，未将它记为全仓额外Any审计通过。SQLite动态结果先接为object再验证，排序回调与
容器明确类型，不使用cast、ignore、新豁免或放宽配置。完整日志留本地
`.e2e-artifacts/ghost-scope-history-20261117/`。未运行全量pytest或已停止语言批次。

## 生产消费者、默认状态与回滚

普通API/CLI的Manifest scope删除直接使用本次实现；inbox、Hebbian、Sleep现有reducer与
observation普通读取/向量检索默认消费相应删除或退役状态，无新增待开启开关。原始历史
仅保留供审计，不作为当前可用来源；不生成Evidence、Permission、CompletionProof或假receipt。
用户关闭、scope隔离、Manifest同ID跳过completed及故障回滚合同保持有效。

事务失败回滚mutation与ack；生产回退优先隔离受影响消费者并保留数据库和永久退役登记。
代码回退须先验证旧版本对新增删除事件的重放兼容性，不能直接降级后让旧reducer忽略
控制事件；也不能清除retirement、复用已删run/candidate身份或用旧快照覆盖当前数据。

## 下一步与限制

低容量普通scope删除缺口已闭合。容量compaction、自动observation裁剪、reset_all及单个
candidate移除仍有物理重写路径；其他三store在超预算删除时也可能触发compaction。
下一实验需要独立冻结阈值、保留历史和投影有界性的对照，不据此宣布完整retention、0.6.3、
确定性replay或World Model完成。之后继续其余升级/异常身份scope与生命周期、显式时间，
再推进真实World Model消费者闭环。
