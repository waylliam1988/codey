# 图与 continuity 逐记录维护生产修复

- 日期：2026-11-18；关联0.6.2/0.6.4；冻结提交 `d293b938`，生产基线 `e5ca1bf7`。
- 复用[四领域原始负结果与修复冻结](maintenance-record-scope-20261118.md)。无模型/provider调用：本轮恢复控制与持久事件契约，沿用既有模型收益范围，不新增模型行为收益声明。
- 状态：`PRODUCTION_MERGED`，本记录与生产实现同一master提交；最终检查结果见下文。用户要求本批交接提交后停止，长期目标保持暂停。

## 正常入口与默认行为

普通App/default Sleep daemon的Hebbian decay、Affinity decay/source sync及continuity refresh默认消费逐记录维护准入，无新增默认关闭开关。控制在owner写锁内重新读取并校验，不缓存上一周期权限。普通post-turn学习继续使用原accept_events职责，维护同步显式传入 `maintenance=True`。

复用不可变policy，Hebbian/continuity使用raw身份，Affinity使用encoded身份，queue维持raw project/encoded session。user deny继承；明确project/session deny保护对应记录。缺canonical父project来源时，任何project deny保守暂停session维护，user及其他允许project继续。

Hebbian只追加实际允许行的upsert、既有精确pruned事件和审计标记；不再用全库snapshot表达一次decay。涉及denied节点的边及其两端均保护，避免孤边。Affinity同时检查边自身scope，对会挤出protected行的来源提案逐项拒绝，允许的安全强化继续。

Affinity新增严格 `ghost_affinity_maintenance_applied` 事件，记录实际decay选择；replay先恢复当时有界投影，再只处理记录的ID。维护source sync用空选择固定容量边界，避免后续重放跨越未落盘的中间裁剪。continuity只过期/刷新允许行，为protected行预留容量，追加实际upsert与严格最终成员ID事件；replay按成员投影，不读取当前控制，不把容量淘汰伪装成过期。

三个owner在任何维护限制期间暂缓全库checkpoint，包括mutation内部自动checkpoint；queue此前已同样处理。继续追加允许事件并保留历史。**这不是有界选择性compaction**，消费窗口可能超预算。显式删除仍由既有删除权威执行，后续维护/rebuild不得复活已删除来源。

## 验收与失败归属

原四领域12格scorer未改；旧有效基线6通过/6失败、queue独立修复后仍6/6。本候选原12格已全部通过。新增96项owner/Sleep/控制/容量/并发/故障/生命周期边界，以及39项严格新事件/rebuild边界。

扩大33文件 **1614 passed / 2 subtests passed**（1240.71秒），无失败/跳过。随后Ruff/类型整理后的11文件 **370 passed / 2 subtests passed**；最终Affinity集合类型整理后扩大至13文件 **439 passed / 2 subtests passed**（65.87秒），包含全部135项新增、原12格及受影响continuity/Affinity/Sleep回归。三个批次分开记录，不将扩大批次伪称为最终源字节的一次全通过；后续改动只有明确类型/导入/具名局部变量及等价成员投影函数提取。

最终相关回归之后串行执行五项指定门全部通过：全仓Ruff、完整702生产文件mypy/Pyrefly/ty、precision gate。八个owned文件三检查器均通过，额外 `--disallow-any-expr` owned零诊断；其未改依赖334文件10864项遗留诊断使额外审计exit1，单独保留，不宣称全树Any清零。全树Pyrefly有158 warnings/35 suppressed，owned有3 warnings，均与error gate结果分开。未添加cast/ignore/豁免、缩小全树检查或放宽配置。最终回归前冻结的702生产+2测试文件hash在最终静态门后全部一致。

新增测试首批69通过/27失败属于夹具错误：continuity preference TTL为21天，原新增边界逻辑时间尚未过期；改为12月2日，不改原12格实际App时间/scorer。另修正Hebbian关系 `coactivated_with` 及source collector必填参数。第二批146通过/1失败属于比较方式误把语义排序当ID排序；双方均按具名typed ID回调排序，保留逐字段内容和重复replay顺序验证。原始失败log/XML/state保留。

候选continuity成员事件原先漏写event_id，新生命周期rebuild验收暴露后修生产生成器，明确写入 `gcm_` 身份；不能因原12格没有rebuild而认为该候选已验收。

静态门的中间失败也保留：Ruff首检两个问题为continuity replay复杂度21超过既定20及测试导入格式，提取typed成员投影函数并整理导入后通过。owned mypy首检四项来自测试同名变量跨Hebbian/Affinity领域复用，改为领域名称。额外Any首检两项是 `issubset` 上下文把生成器推断为 `Generator[Any]`，改为先构造明确ID集合；最终13文件回归与所有静态门在这些修改后完成。

可复现入口：

```powershell
python -m pytest tests/test_ghost_selective_maintenance.py tests/test_ghost_selective_maintenance_events.py tests/test_ghost_maintenance_record_scope.py -q --tb=short
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
```

完整扩大命令、owned三检查器/额外Any审计及脱敏指标见[生产报告](../../tests/manual/ghost_memory_routing/reports/selective-maintenance-production-20261118.json)。完整log/XML/SQLite/hash仅留本地 `.e2e-artifacts/ghost-selective-maintenance-20261118/`，不入仓。未跑全量pytest，未涉及前端。

## 限制、隔离与回滚

1. 受限checkpoint窗口有界化、物理retention/purge未闭合。不要用删原始日志解决增长。
2. 未知父project的session保守暂停；任意旧非canonical/超长/raw hash-shaped身份alias来源未完整证明。Affinity既有24位小写hex判为已编码行为不能推广为所有旧身份安全。
3. 新Affinity事件被旧validator拒绝；旧continuity reader可能忽略未知成员事件。未证明无缝降级或混版本。保留兼容reader和SQLite原始历史；不能直接revert后让旧reader重建新日志。
4. 需要故障隔离时先用现有全局run_maintenance控制停止维护，空闲重启并保留原始state；代码回退需同时保留新事件reader或使用另行验证的状态迁移。不回退到错误维护行为后宣称控制仍安全。
5. pending daemon payload交接/硬退出、混版本及完整生命周期/G0未完成。World Model新候选仍需新增预测信息，两个重复当前verification事实的已终止候选不重跑。

本批结束后的精确接手入口与停止状态见[批次交接](selective-maintenance-handoff-20261118.md)。
