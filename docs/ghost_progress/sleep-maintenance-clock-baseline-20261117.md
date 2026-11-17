# Ghost Sleep 维护周期取时基线

- 状态：`EXPERIMENT_COMPLETE`（原始负结果，后续显式维护时间已限定闭合）。
- 关联：0.6.4；基线生产 commit：`648c5a9e`。
- 不调用模型，不恢复已停止的语言实机批次；不修改生产代码。

## 冻结目标与矩阵

同一初始领域记录、同一周期入口时间 `2026-04-01T00:00:00Z`，Sleep 后续
墙上时钟变化不应改变本周期的维护决策。用真实 store、事务、普通 `run_once` 和重开后的
持久投影比较；不以新 API 是否存在作失败依据。

三 scope（user/project/session）× 五消费者（Hebbian 衰减、Affinity 衰减、continuity
刷新、work queue 租约回收、Affinity 对过期 work item 的来源读取）共15 cell。
每 cell 各运行恒定、恒定复验、入口后跳至7月1日、入口后退至1月2日四个独立数据目录。
只复制类型明确的初始领域事件/记录，不复制仓库或 worktree。

所有初始事件使用固定 ID 和1月1日时间，project 身份在四个目录中相同；Affinity 使用
生产 scope 编码。恒定复验必须完全相同，且恒定对照的相关步骤必须产生真实状态变化。
逐 cell 输出实际行、projection digest、步骤 counts 和 skipped_reason，前跳/后跳均与恒定对照比较。
cycle/event UUID、实际运行耗时、报告开始结束审计时间和导出 generated_at 不参与比较；
记录内的更新、衰减、TTL、lease 等行为时间参与。时钟只替换 `_common.now_iso_z`，
第一调用固定为周期入口，之后改变；不替换纯解析/衰减函数。

成功条件为15/15 cell在前跳与后跳下均一致。来源过期、租约是否过期、权重、状态或TTL不同
均为生产决策差异。真实生产通常使用运行时取时；本实验不证明通常运行必然发生时钟跳变，
也不证明完整显式维护时间、所有领域 replay、实际模型收益或 World Model 完成。

## 命令与结果

`python -m pytest tests/test_ghost_sleep_clock_boundary.py -q --tb=short`：
**15 failed / 1 passed**。15 cell均到达最后比较断言，恒定复验和真实变化对照全部通过；
15项前跳及15项后跳均产生实际差异。没有夹具失败或缺失cell，不把时钟夹具通过算成矩阵通过。

user示例（其余两个scope结论相同）：Hebbian权重恒定/前跳/后跳为
`0.4 / 0.198466 / 0.793862`；continuity TTL分别为4月22日/7月22日/1月23日；
两条租约分别回收1/2/0条。Affinity来源恒定可读的work item，在前跳后被过滤，未产生节点。
这些是行为字段与真实持久状态差异，不能归为随机ID或报告审计时间差异。

相关现有五文件回归：**136 passed / 2 subtests passed**。全树Ruff、生产树mypy/Pyrefly/ty、
precision gate均通过。新增实验脚本单独通过三类型检查，额外Any审计owned零诊断；
未改依赖184文件/4010诊断导致额外审计exit1，不声称全仓Any清零。
运行期间702个生产Python文件已冻结并复核哈希相同，无生产改动。

脱敏指标与实际命令：[JSON报告](../../tests/manual/ghost_memory_routing/reports/sleep-maintenance-clock-baseline-20261117.json)。
原始XML、stdout、冻结哈希及检查日志在本地`.e2e-artifacts/ghost-sleep-clock-20261117/baseline*`。

## 下一步

[后续生产接线](sleep-maintenance-time-production-20261117.md)已让本15-cell矩阵全部通过，
显式逻辑时间在普通Sleep及来源过期筛选中默认生效；原始负结果与冻结条件保留。
下一步继续过期记录的持久清理/重建、稳定排序/digest及其他领域replay，不重做本矩阵。
