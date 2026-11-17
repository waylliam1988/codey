# Ghost continuity 过期持久事实与消费者生产接线

- 日期：2026-11-17；关联：0.6.4/0.6.3。
- 原始负结果 commit：`92276fec`，生产基线：`405410f5`。
- 状态：`PRODUCTION_MERGED`（限定continuity过期持久事实与实际消费者）。
- 生产提交定位：`git log --grep="persist continuity expiry and filter real consumers"`。
- 模型/endpoint：不使用；本次修复确定性的过期、重建和事务契约，不新增模型收益主张。

## 冻结目标与实现

保留[原18-cell实验](continuity-expiry-baseline-20261117.md)的所有断言、初始事实和时间，
不更改TTL、confidence、排位、队列生成条件或digest。成功维护后的持久行必须与报告一致；
固定领域事件重建不得恢复已移除的过期项；未维护的普通读取不能用过期项创建新工作。

continuity同步先识别实际存量中的过期ID，以原始存量与最终投影比较，不能先滤掉过期项
再声称没有变化。追加有界的`ghost_continuity_items_expired`事件，保存明确逻辑时间和
排序后的ID集合；随后追加同轮新事实upsert及同步报告。事件、投影、consumer cursor及
可能发生的容量checkpoint处于同一事务。changed计数按不同ID计算，同ID过期后重新生成
只计一次。过期不调用永久来源退役；仍保留用户删除的既有来源退役语义。

重放检查expiry事件的schema、时间、payload、ID类型/规范性/唯一性及120项上限；
损坏时拒绝重建并保留上一有效投影，不猜当前时间。只移除在事件时间已过期的当前ID，
缺失ID无副作用，重新出现且尚未过期的事实不被旧expiry事件删除。
旧版混合新增可能已经写出正确投影，但只有upsert与`ghost_continuity_synced`、没有expiry
事件。重放同时消费该既有同步事件记录的维护时间，筛掉当时已过期项；不以当前墙上时间
或报告总数猜测删除。仅修复新写口不能覆盖这个已存在的升级输入，故独立补测。
容量checkpoint继续采用已有领域窗口，原始SQL历史不物理删除；维护内checkpoint使用
传入的逻辑时间，不增加隐藏取时。没有新增模块、schema迁移或第二份权威日志。

`list_items`、builder和renderer新增可选`maintenance_as_of`，独立入口省略时捕获一次，
显式空值/坏值拒绝；builder共用同一时间完成读取和渲染。普通reader只筛选、不清理持久行，
`export_state`仍可审计未维护的原始投影。work queue已有入口捕获的`now`传入真实continuity
读取，过期来源不生成新的工作；已存在的独立工作不因来源TTL而自动撤销。

为接通实际消费边界，清理`work_queue_sources.py`全文件Any：continuity、research候选、
checkpoint与ledger使用具体领域类型，直接读取已声明字段，terminal event保留明确Mapping
边界。原source所有权测试改用真实SQLite/domain fixture和具名typed回调，保留五项行为
断言；不为过时SimpleNamespace签名增加生产兼容分支、动态字段或导出。

## 验收与失败归属

首批候选：205通过、3失败、2子测试通过。原18-cell矩阵全部通过；新增同来源复活的三scope
测试遗漏`mode="chat"`，合法默认空mode不会生成task focus，因此在夹具正对照处失败。
补齐真实调用条件，未放宽断言或改变生产生成条件；保留首轮失败日志。

首批扩大37文件897通过、2子测试通过，全树静态门通过；随后旧日志补测9项中3个过期前
对照通过、6个at/after重建失败，确认候选仍缺升级输入。按旧同步事件的显式时间修正
重放后，六文件验收227通过、2子测试通过，包含原18-cell、75项新增验收和五项来源所有权。
首次测试类型审计还发现关键字字典无法确定参数名（mypy/ty各9项）及空容器隐式Any
（Pyrefly1项），已改为明确签名的refresh回调和容器元素类型，未增加豁免或放宽生产参数。

新增验收覆盖同来源新事实/重复同步、逻辑时间与禁止隐藏取时、两类容量checkpoint和原历史
保留、append/replace/checkpoint写入失败后的事务回滚与重试、坏事件和坏显式时间、默认
读取/builder/renderer/实际队列只捕获一次时间、纯重放保护已刷新的记录及取消传播。

最终44文件生产回归：**1064 passed / 4 subtests passed**，无失败、错误或跳过；覆盖上述
验收及普通Chat/Research发送、post-turn、准入/四开关、scope、删除/reset、来源退役、
原始历史/容量窗口、损坏/取消、重开、manifest恢复/并发和限定混版本边界。
原18-cell由3→18通过，旧日志9项由3→9通过；不累加批次数量。

最后相关回归后串行全树Ruff、生产树mypy/Pyrefly/ty及precision全部通过。
生产Pyrefly为0 errors、35既有suppressed、158 warnings，与原基线相同。
两个生产文件、两个新增/修改测试及未改基线共五文件另过三类型检查，owned Pyrefly无警告，
额外Any零诊断。额外检查仍因未修改依赖181文件3873诊断exit1，不声称全树Any清零。
无cast、ignore、新豁免、关闭检查或未经验证的动态字段访问。
所有runtime批次均冻结702个生产Python文件并复核不变；未运行全量pytest。

完整最终命令、退出码、18项脱敏测量、旧日志结论及五文件LF源码哈希见
[脱敏报告](../../tests/manual/ghost_memory_routing/reports/continuity-expiry-production-20261117.json)。
完整日志、JUnit、各批冻结哈希和失败记录保留在本地
`.e2e-artifacts/ghost-continuity-expiry-20261117/`。

```powershell
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
```

## 默认消费者、回退与限制

现有AppContext→Sleep→continuity维护、post-turn同步、普通continuity builder和work queue
来源转换直接消费此逻辑，无新增默认关闭开关。已有四开关、scope隔离和用户主动关闭保留，
不恢复已停止的语言实机批次，不增加后台模型调用。写入失败沿用事务回滚，Sleep报告失败；
未知或损坏输入不会冒充成功。

代码回滚可revert本次生产提交，但旧代码不识别新增expiry事件；操作前应保留数据副本，
避免用旧replay覆写有效投影。原始历史继续保留，数据恢复应使用理解expiry事件的reader
和明确逻辑时间，不应删除过期历史或永久退役仍可更新的来源。

本次只闭合continuity过期与新工作生成边界；不宣称已有独立work item随来源TTL撤销、
旧版本writer/reader全隔离、旧快照/进程内存撤销、磁盘purge、稳定排序/digest全域验收、
整个0.6.1–0.6.4或World Model完成。基础边界之外的事件/预测/复盘/真实消费者仍需继续。
下一步继续其余维护入口、稳定排序/digest与领域replay，以及真实发送/controls尚缺的完整
验收；不重做本次已闭合矩阵，随后推进真实World Model消费者和效果评估。
