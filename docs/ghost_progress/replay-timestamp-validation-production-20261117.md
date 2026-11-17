# Ghost 重放时间校验生产修复

- 日期：2026-11-17；原始负结果：`09a03b31`。
- 关联：0.6.4；状态：`PRODUCTION_MERGED`（限定历史时间校验与重建回滚）。
- 模型/endpoint：不使用；这是固定日志重放与损坏隔离边界。

## 冻结目标与实现

保留原始12-cell基线、对照和确定性断言，不增加时钟、不改权重阈值。候选需将九项
坏时间场景改为明确拒绝且保留上一有效投影；三个合法时间场景继续实际重建并保持
确定性。额外校验五种Affinity事件的坏ts、快照节点和边的四种坏时间字段、无基点、
合法旧快照可选空字段、UTC/naive/offset历史格式、数学函数错误传播、随机事件ID
不影响投影、普通Sleep记录失败且重开后可读上一投影。

共享graph_primitives移除datetime.now回退和吞错后返回旧权重/skip的路径；坏时间
抛ValueError，正常ISO时间维持UTC转换，旧naive时间仍按UTC。Affinity应用事件前
校验ts和快照时间，拒绝发生于state字典建立或替换之前；完整重建沿用原SQLite事务
回滚。原始坏日志保留供审计和显式处理，不猜测历史时间、不物理清除坏事件。

两个修改生产文件全部清理既有Any：图裁剪用只读领域Protocol约束节点/边，保持
原排序与阈值；Affinity replay用明确的节点/边TypedDict，动态payload在边界收窄，
移除动态import。无新生产模块/配置/默认关闭开关；正常rebuild、衰减和Sleep直接调用。

旧graph primitives测试要求坏时间返回当前时间，与确定性契约冲突；保留其合法时间
roundtrip和所有数学、裁剪、夹紧检查，将坏时间断言改成拒绝，并用新故障测试补足
传播、原子性及真实消费者。不能让生产为这个旧断言继续猜测时间。

生产Python在每个测试批次冻结并复核全部哈希。先运行候选及相关回归；失败区分
夹具/类型/生产归属，不删除断言或放宽scorer。通过后扩大到时间解析受影响的
continuity/work_queue/directive及删除、retention、控制和恢复边界，再串行完整静态门。

## 验收结果

候选 **211 passed**：原12-cell矩阵全部通过（九项坏时间明确拒绝、三个合法重建通过），
另一个时钟夹具通过；71项新增边界与相关既有图/损坏/Sleep/Hebbian验收全部通过。
生产702个Python文件哈希保持不变，candidate.xml及日志保留本地。

第一轮扩大回归为 **1194 passed / 1 failed / 36 subtests passed**；失败为既有
`test_ghost_disabled_preserves_chat_execution_without_learning` 在 TemporaryDirectory
退出、SQLite文件已删除之后调用read_committed，得到unable to open database file。
生产sqlite连接与目录生命周期本次未改，错误发生在测试资源已经销毁后，归属测试。
改为目录存在时读取并保存observations，再保留原来的空元组断言；没有让生产为已删除
目录创建空数据库，也未删除、跳过或放宽“关闭学习不新增记忆”的验证。

按本轮文件Any清零要求，一并修正该旧测试模块184项owned类型/Any诊断：明确回调签名、
直接引用AppContext、提供正常可追踪的provider子类、收窄终态/可选store、在SSE与Mock
参数边界校验动态映射。保留所有12个用例的工作流、发送次数、取消/失败、禁写、review
及snapshot diff断言。普通任务行为没有因类型修正而改成stub执行器。

首轮全树mypy还发现有界TypeVar内直接对两个Protocol作交叉收窄被判unreachable，
已将相同运行时状态检查提取为GraphRow参数的布尔函数；没有变更状态谓词。额外Any
审计发现旧unittest列表上下文及新测试的sorted lambda发生Any推断，已明确容器和
回调类型。六个受检文件的针对性三检查器与额外Any审计owned零诊断已通过。

最终源码复验 **523 passed / 36 subtests passed**，无失败/错误/skip，覆盖这次类型/夹具
修改和受影响正常入口、发送边界及共享时间消费者。生产702个Python文件在整个批次
哈希不变。随后串行全树Ruff、mypy（702模块）、Pyrefly、ty和precision均通过；
Pyrefly为0 errors、35项既有suppressed及158 warnings。原始大批次、静态失败及修正日志全部保留，
不把原1194/1写成1195全通过，也不将不同批次样本相加。

本次五个修改/新增Python文件及原基线脚本共六文件另经mypy、Pyrefly、ty通过，
额外Any审计owned零诊断。未修改依赖338文件11025项诊断使扩展Any命令exit 1；
不能将该结果写成全树Any清零。无casts、ignore、新豁免、放宽范围或关闭检查。
完整24文件复验命令及逐项exit code见脱敏报告checks；全树门按以下顺序执行：

```powershell
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
```

首次类型预查发现Hebbian GhostEdge没有status，而AffinityEdge有；已将共享Protocol
区分为权重/时间、节点状态和边端点，只有require_active路径收窄状态能力。没有强迫
Hebbian新增无意义字段，也未改变原裁剪、权重和degree语义。候选运行前已修正。

原始负结果保留；本次不等于完整显式maintenance_as_of、所有领域重放、
整个0.6.4或World Model闭环完成。普通Sleep仍需后续显式逻辑时间改造。
回滚保留的是事务开始前的投影；本矩阵已确认其有效，不证明任意既有损坏投影会被
自动修复或重新认证。坏日志与此前已经写入的错误状态需要独立诊断，不能用当前时间补齐。

## 生产消费者、默认状态与回退

正常Affinity事件应用、rebuild、Hebbian/Affinity衰减及共享时间消费者直接使用校验；
普通Sleep失败报告与重开后读取旧有效投影已有真实store验收。没有新增默认关闭开关、
后台模型调用、第二份权威日志、数据库迁移或新生产模块；无需发布新增模块清单。
本次没有新的模型行为晋级，沿用此前受测中文功能默认启用，不恢复已停止实机批次。

问题发生时保留原始事件和事务前投影，通过已有warning定位失败，隔离受损消费者后
用明确历史事实修复并重建；不能以重新启用wall clock猜测作为数据恢复。代码回退可
单独revert本次修复，但会重新暴露已证实的非确定性，不应当作坏历史的修复办法。

完整日志、JUnit、source freeze和各检查输出留在本地
`.e2e-artifacts/ghost-replay-clock-20261117/`。仓库保留
[脱敏矩阵与检查摘要](../../tests/manual/ghost_memory_routing/reports/replay-timestamp-validation-production-20261117.json)。

下一步继续显式maintenance_as_of、稳定排序/digest与其余重放边界；不据本次修复宣布
完整0.6.4、0.6.1–0.6.3或World Model完成。
