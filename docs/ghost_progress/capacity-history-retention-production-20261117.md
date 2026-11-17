# Ghost 容量检查点与原始历史保留

- 日期：2026-11-17；生产比较基线：`7244c1e3bd6c44c645f43209aa834cd0cd759c75`；
  负结果提交：`265d9fe020404ef35ff762750d48c38c28b8b358`。
- 状态：`PRODUCTION_MERGED`（本记录随独立生产修复提交到本地master）。
- 范围：已有领域的自动容量compaction、普通超阈值scope删除、Sleep维护和observation追加。
- 模型：不调用；不恢复停止的语言批次，不新增模型行为收益主张。

后续：[普通reset历史保留](reset-history-retention-production-20261117.md)已独立生产修复并
默认接线；本文关于reset待验为本批次当时限制，不表示当前仍缺该63项矩阵。混版本旧进程、
物理purge/磁盘策略及完整0.6.3仍未闭合。

## 合同与取舍

保留同一领域原表中的sequence、event_id和payload；compaction在相同事务追加有界状态
checkpoint，并推进该领域的排他重放边界。原有events()与event_stats()读取当前重放窗口，
当前projection/consumer cursor仍与事件提交原子一致；历史通过原领域SQL表审计，不新建
第二个权威日志。快照envelope使用独立ID，避免重复维护复用原事件ID导致冲突或去重吞掉
新检查点；随机ID不作为后续确定性projection digest的排序依据。

schema 8仅增加七个领域的窗口元数据，7→8初始边界为0，保留既有数据可见性；schema 8
缺表、缺领域窗口或窗口anchor丢失不默认回退到从头重放。schema 7冷启动会拒绝新版本，
不把这等同于已运行旧进程也有跨版本并发隔离。领域检查点、窗口、projection与Manifest ack沿用同库事务。
窗口同时保存after_sequence与applied_after_sequence，projection确认时同步二者；未确认窗口
即使事件尾号不变也拒绝提交。缺失anchor与未确认位置分别fail closed，不回退为全历史重放。

observation原事实不再随满容量追加物理删除；公开reader仍最多采用最近5000条的原有窗口，
read_committed仍先按scope/receipt/退役过滤，再受请求limit约束。窗口推进单调，删除较新
scope不能把窗口外旧scope回填进来；scope delete仍退役所有匹配身份，计数只含窗口内live行。
过期派生向量清除；embedding I/O后再次核对窗口，不能把被窗口排除的旧事实重新索引或返回。

Sleep报告仅推进已有事件的尾窗口，不复制成百上千条旧report。按现有条数和bytes预算
同时决定边界，保留最新报告；最新单条报告本身超预算则拒绝并回滚，不提交一个与projection
不符的空窗口。该操作仅允许Sleep使用，不能把其他来源投影误当报告尾部截断。

此方案限制当前重放/检索工作集，**不限制原始历史的磁盘总量**。物理purge与保留期限需要
独立可审计策略；不能用默默丢历史来宣称容量已完全治理。本次reset仍有既有物理删除路径，
不据此标记完整retention或0.6.3完成。
本次生产范围为正常单版本runtime与冷启动升级；旧进程跨版本并发/降级仍需独立验收，
不能用当前同版本多进程与硬退出恢复结果替代。

## 验证与失败归属

原78项容量矩阵从48通过/30失败变为78全部通过；与普通历史保留、存储、经历记忆、向量
检索首批合计 **212 passed**。这批之后只调整相同语义的schema guard结构和import顺序，
最终相关回归另行记录。

新增窗口验收覆盖重复/空checkpoint、事务失败回滚、窗口缺失与anchor损坏、真实schema7
升级、检索窗口有界及embedding期间窗口前移。早期类型预检指出空tuple分支的参数类型
需要显式声明，不通过cast或ignore处理。

既有升级测试通过“最新schema建库后人工设置旧user_version”构造旧库；新窗口表原本不应
存在于这些旧版本fixture。保持原升级、防绕过、来源退役和原generation断言，显式移除
schema8元数据后再模拟旧库；schema升级结果断言从7更新到8，不让生产迁就不完整旧夹具。

最终结果、命令、默认状态和回滚结论见下文；本节保留候选失败归属，不能只保留通过批次。

首批扩大回归为 **348 passed / 50 failed / 2 subtests passed**。49项来自上述人工旧库夹具；
另1项是真实生产发布清单遗漏：新增模块未进入runtime.json，启动快照正确拒绝不完整inventory。
已运行 `python -m tools.publish_runtime`，清单仅增加新模块；未放宽launcher校验或修改启动测试。
额外Any预检发现两个改动旧测试中3项既有动态表达式，改为明确SQL Row边界和typed JSON对象，
不削弱原有schema、退役和拒绝断言。

检查点候选还发现空快照确认漏洞：六领域各一项未配对append((), replace=True)，均未拒绝。
事件尾号不变不足以证明窗口已随projection确认；需要独立窗口确认位置，未确认时同事务回滚。
这属于本次候选实现问题，不将它描述为原生产基线发生过消费者复活。

第一次完整复验为 **756 passed / 1 failed / 136 subtests passed**。余下一项旧库测试在
人工降级后调用最新writer；窗口缺失时最新writer正确拒绝，不能代表schema6的历史producer。
改为按实际旧schema写入事实，保留“未知新记录不能在LIMIT前挤出有效旧记录”的原断言。

Sleep bytes三scope候选检查均失败：事件条数压到1000后仍约1.44MB，超过既有512KiB预算；
把旧尾部复制为新checkpoint还会放大历史写入。改为直接推进已有事件窗口、同时按count/bytes
限制；新增连续维护仅新增一个report的断言，不通过关闭维护或缩小原始历史掩盖问题。

修复窗口确认与Sleep尾部后，新增53项窗口验收连同legacy准入、Sleep及Manifest原子性共
**174 passed**。随后仅移除改动后不再使用的Sleep事件重写方法，anchor损坏fixture同时修改
窗口与确认位置，以单独证明anchor校验。最终完整复验另见下方，不用174项替代全范围验收。

十三个owned文件的额外Any审计零诊断；未修改依赖349文件11565项诊断使扩展命令exit1。
未新增cast、ignore或检查豁免，不把该扩展命令说成全仓Any审计通过。

## 最终结果与命令

SQL窗口复查前的扩大回归 **776 passed / 136 subtests passed**。随后“窗口读后、SQL前”交错
两项对照1通过/1失败：旧向量缓存被重新插入，查询仍拒绝，不声称有查询泄漏。改为实际
INSERT/排名语句内复查当前窗口后2项通过；额外类型检查指出测试不应依赖其他模块的隐式
re-export，改为从定义模块引用原函数，不扩大生产API，最后2项复验通过。

最终源码相关复验 **366 passed**，无失败、错误或skip。原78项矩阵全部通过，
另有55项窗口验收全部通过；两组均从最终JUnit逐case核对。776项扩大与最终366项是独立批次，
不能相加或将776说成包含最后两项交错的最终全范围结果。[脱敏报告](../../tests/manual/ghost_memory_routing/reports/capacity-history-retention-production-20261117.json)
保留原矩阵、候选缺口与最终汇总，不保存原始事件、临时路径或state。

```powershell
# SQL复查前的扩大回归历史命令；当前源码执行还会包含后增两项交错。
python -m pytest tests/test_ghost_capacity_history_retention.py tests/test_ghost_scope_history_retention.py tests/test_ghost_event_windows.py tests/test_ghost_sqlite_store.py tests/test_ghost_corruption_contract.py tests/test_ghost_graph_sqlite.py tests/test_ghost_canonical_input_receipts.py tests/test_ghost_legacy_projection_admission.py tests/test_ghost_deleted_input_fence.py tests/test_ghost_deleted_source_lifetime.py tests/test_ghost_inflight_input_lifetime.py tests/test_ghost_manifest_atomic_mutation.py tests/test_ghost_manifest_production_wiring.py tests/test_ghost_deletion_manifest_recovery.py tests/test_ghost_deletion_manifest_multiprocess.py tests/test_ghost_inbox.py tests/test_ghost_hebbian.py tests/test_ghost_sleep.py tests/test_ghost_continuity.py tests/test_ghost_work_queue.py tests/test_ghost_affinity.py tests/test_experience_memory.py tests/test_ghost_vectors.py tests/durable/test_operation_driver_restart.py tests/durable/test_legacy_chat_rescue.py -q --tb=short --junitxml=.e2e-artifacts/ghost-capacity-history-20261117/production-complete-final.xml
# 最终源码的相关复验。
python -m pytest tests/test_ghost_capacity_history_retention.py tests/test_ghost_scope_history_retention.py tests/test_ghost_event_windows.py tests/test_ghost_vectors.py tests/test_experience_memory.py tests/test_ghost_legacy_projection_admission.py tests/test_ghost_canonical_input_receipts.py tests/durable/test_operation_driver_restart.py tests/durable/test_legacy_chat_rescue.py -q --tb=short --junitxml=.e2e-artifacts/ghost-capacity-history-20261117/production-sql-final.xml
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
```

最终复验后的静态门全部依次通过：mypy覆盖702个生产模块；Pyrefly为0 errors、35项既有suppressed、158 warnings。
六个修改测试另经三种检查通过，十三个owned文件额外Any审计结果如上：

```powershell
$ownedTests = @(
  'tests/test_ghost_event_windows.py',
  'tests/test_ghost_canonical_input_receipts.py',
  'tests/test_ghost_legacy_projection_admission.py',
  'tests/test_ghost_deleted_input_fence.py',
  'tests/test_ghost_deleted_source_lifetime.py',
  'tests/test_ghost_inflight_input_lifetime.py'
)
$ownedProduction = @(
  'codey/ghost/event_windows.py', 'codey/ghost/graph_store.py',
  'codey/ghost/record_store.py', 'codey/ghost/sqlite.py',
  'codey/ghost/observations.py', 'codey/ghost/vectors.py', 'codey/ghost/sleep.py'
)
python -m mypy @ownedTests --python-version 3.11
pyrefly check @ownedTests --python-version 3.11
ty check @ownedTests --python-version 3.11
python -m mypy @ownedProduction @ownedTests --python-version 3.11 --disallow-any-expr
```

原始日志、失败state和JUnit留本地 `.e2e-artifacts/ghost-capacity-history-20261117/`。
未运行全量pytest，未重跑已停止语言批次，未调用模型服务。

## 默认消费者、回滚与剩余缺口

现有领域compaction、默认Manifest scope删除、Sleep维护及observation append/read/retrieve
直接使用新窗口，无新增默认关闭的开关或无人调用模块。runtime发布清单已同步，新启动和
恢复测试实际经过该清单。原有显式关闭、scope、retirement、quarantine及删除代际fence保留；
不改Evidence、Permission或CompletionProof，不从历史日志直接增加模型可见内容。

失败时窗口、checkpoint、projection和Manifest确认在既有同库事务内回滚；原事件保留。
生产回退优先隔离受影响消费者并保留数据库及永久退役登记。schema8不能直接降为7或手工
归零窗口，否则旧reader可能重新采用归档来源；需要受控迁移和独立验收，不能只修改PRAGMA。

下一步验证reset的tombstone历史合同、已运行旧进程的跨版本边界、物理purge/磁盘retention
及其余升级/异常身份scope和完整生命周期；随后继续显式时间、确定性replay及真实World Model。
当前生产启用范围不等于这些未验边界已完成，0.6.3与World Model继续保持未完成。
