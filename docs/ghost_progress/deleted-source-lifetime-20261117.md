# Ghost 删除后重新读取来源的生命周期

- 日期：2026-11-17
- 基线：`7eded52e`
- 实验状态：`EXPERIMENT_COMPLETE`（限定来源重新消费负结果）
- 实验提交：`433207a9`
- 生产结论：`PRODUCTION_MERGED`（限定 continuity/work queue 的来源身份，生产提交见下）
- 模型 / endpoint：无，不恢复已停止的语言批次。

## 固定假设和矩阵

删除 Ghost 投影后，原 runtime 事实仍由原领域持有；重新读取相同来源，不应重学
用户明确删除的投影。相同来源仅修改文字也不能绕过删除；新的 run 身份仍应可用。

初始矩阵：两个消费者（continuity task source、真实 WorkCheckpointStore）× 两个
操作（session delete、reset）× 原文/同身份改文，共八项；另以 continuity 覆盖
user/project × delete/reset，四项，总计 12 项，各一次。每项独立临时 state。
delete 后先重启并 rebuild，确认空投影，再重新消费来源。checkpoint 原始事实必须保留。

停止条件：任何旧来源投影复活即保留阴性结果；不通过删除 runtime checkpoint、放宽 scope
或 scorer 过门。这验证持久来源身份，不替代未落盘在途输入或完整并发生命周期。

## 基线结果

```powershell
python -m pytest tests/test_ghost_deleted_source_lifetime.py -q --tb=short
```

初始 12 项全部失败：delete/reset 成功，重启后的 rebuild 为空，但重新读取旧 task source
或 checkpoint 后又出现相同 run 的投影。八项还覆盖仅改变旧来源文字的情况。
完整基线输出留在本地 `.e2e-artifacts/ghost-control-failures-20261117/source-lifetime-baseline.log`。
回归脚本随随后生产修复提交，基线源码可从上述 Git commit 读取，无需复制 worktree。

这不是 projection generation 的失败：重新消费发生在删除后，取得新 generation，
因此旧的在途快照 fence 按设计不会拒绝。缺口是已删除来源的持久退役语义。

## 生产修复

Ghost SQLite schema 5 新增持久来源退役集合，按
collection/source/source_ref/scope/scope_ref 隔离，明确 delete/reset 时与删除同事务写入。
continuity/work queue 的正常 sync 和 rebuild 状态写入过滤已退役来源；SQLite insert/update
trigger 拒绝换一个 record id 绕过退役。正常生产入口默认生效，没有新增实验开关。
查询按本轮候选逐项使用主键，不扫描不断增长的全部历史退役记录。

普通过期和容量筛选不退役；空 source_ref 不能猜测身份。runtime checkpoint、ledger、
knowledge 等原领域事实不删除；新 run 及其他 scope 仍可生成有效投影。

扩大回归发现首版候选误将 Hebbian node id 当成永久来源身份，使删除后新手工确认不能
恢复 continuity，两项新增回归均失败。这是候选生产代码的身份建模错误，未修改测试迁就。
现已排除 Hebbian 槽位：它的真实输入身份继续由 candidate retirement gate 管理，新的
candidate 可重新产生相同偏好槽位。已保留两项回归，历史输入 fence 和 generation fence 不变。

## 验收

- 初始 12 项旧来源复活回归全部通过；最终本文件 **25 passed**，增加不同 scope、compaction、
  再 reset、SQL insert/update 防绕过、v4 升级、丢失 retirement 拒绝冷启动、新手工确认等。
- 其中四项通过 spawn 独立进程，子进程先打开 owner、主进程完成 delete/reset，再释放消费者；
  两个消费者均拒绝重新投影旧来源。这不是完整七 store mixed-process recovery。
- 13 个受影响文件最终 **288 passed、2 subtests passed**；另八个发送、语言、inbox、Hebbian、
  controls、manifest 和异常边界文件在最终代码上 **137 passed、133 subtests passed**。
  两组共 **425 passed、135 subtests passed**，21 个明确选择的文件。
- 全仓 Ruff、完整 codey mypy（699 模块）/Pyrefly/ty 和 kernel precision 通过；两个修改测试文件
  单独通过三个 checker。Pyrefly 全树为 0 errors、35 个既有 suppression、158 个 warning；
  两个目标测试为 0 errors。没有新增 Any、cast、ignore、豁免或改弱检查配置。
- 没有运行全量 pytest，没有创建/复制 worktree，没有恢复用户停止的语言实机批次。

v2 upgrade fixture 同步移除 v5 的 source 表/trigger 后才模拟旧库；原升级能力断言保留。
新 v4→v5 回归确认已有 generation 保留，不因升级重置旧 fence。

生产提交：`git log --grep="retire deleted Ghost projection sources"`。
回滚必须保留 source/input retirement 和 generation；旧 schema 程序会拒绝 schema 5，
不能删表、清库或丢弃删除身份强行降级。升级前已经删除、且没有来源身份记录的数据不能补猜。

## 剩余边界

本轮只退役明确删除时已存在且具有 source_ref 的 continuity/work queue 来源。
尚未生成任何 Ghost 投影的在途输入、未标识来源、source identity 的既有长度约束、完整 manifest
默认调度、七 store 混合并发及故障恢复仍需继续推进。永久身份集合的增长/维护成本也需度量；
本轮没有延迟或磁盘收益结论，不宣称完整删除生命周期或 World Model 已完成。
