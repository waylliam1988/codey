# 跨 scope 自动维护的记录保护

- 日期：2026-11-18；关联：0.6.2、0.6.4。
- 状态：`EXPERIMENT_COMPLETE`（有效负结果；queue随后独立修复，其余三个领域未修）；生产基线 `9ef0f712`。
- 无模型/endpoint/temperature：验证普通 App 的控制与持久记录边界，不宣称模型行为收益。
- 依赖：触发 scope 在途撤回和控制 fallback 已由 `06c8cfc4`/`5407430f` 默认修复，不重复实现。

## 冻结实验

静态疑点：Sleep 逐步骤控制只读触发 scope，而图衰减、来源同步、continuity 刷新及 queue
租约回收遍历全库。另一个仍允许的 scope 触发普通 Sleep 时，可能维护明确关闭的记录 scope。

固定 project/session 两种目标 scope × user/另一个 project/另一个 session 三种触发
scope ×目标维护开启/关闭，**12 cell，每项一次**，同一 cell 中实际使用四个领域：
Hebbian、Affinity、continuity、work_queue。独立 SQLite + 真实 AppContext/默认 daemon，
不替换维护结果、不调用模型、不用自制 loop 代替普通入口。

每个 cell 种入目标与同类允许邻居：有效 Hebbian preference、两节点/一条边的 Affinity
事实、两个过期 running queue lease；continuity 从空状态真实消费这些已有事实。
时间固定为 fixture 的已知过去 `2026-11-10T00:00:00Z`，lease 到期 `2026-11-11T00:00:00Z`；
普通 App 维护使用实际入口捕获的时间，必须晚于这些日期。不同记录身份、原事件身份不冲突。
各领域保持自己的 scope_ref 编码（Hebbian/continuity raw，Affinity encoded；queue session
encoded、project raw，按现有生产 converter/filter 契约）。
允许邻居与触发身份均不受目标 deny；其余三个控制保持开启。

在真实控制提交后、kick 前保存目标/邻居的四领域持久行快照。主要指标：每领域目标行
是否变化、邻居行是否变化、queue lease 实际状态、正常报告成功/取消/错误状态。
开启目标：四领域目标及邻居均必须实际变化。关闭目标：四领域目标完全不变，邻居仍须
实际变化，报告不因别的 scope 关闭而取消整个周期。不能以全局跳过/无数据/全局关闭通过。
fixture 无效、时间不满足、daemon/health 错误、邻居无变化一律记 error，保留原证据。
12 项有效完成后独立提交负结果；若复现，冻结最小修复，再用原 scorer 验证。

本轮主要验证记录显式 scope 的维护资格，不把触发 scope 的权限当作其他记录的权限。
不把预测、hash 相似或不可逆 ref 当成父 project 来源证明；session 与 project 的父子
约束如缺 canonical 来源，必须另行解决/明确拒绝，不猜测归属。本矩阵没有容量压力，
不证明 scope 选择性 compaction；全局 checkpoint/retention 的 scope 语义、跨边端点、
容量裁剪、并发撤回、rebuild 和重启属于修复晋级前需分别冻结的相关边界。

停止条件：矩阵完成或 fixture error。收益门是恢复本来有效的控制契约且允许邻居继续
维护；原有模型收益证据复用。本轮不以重跑模型实验替代记录隔离，不放宽 scorer。
原始 SQLite/日志/XML 留 `.e2e-artifacts/ghost-maintenance-record-scope-20261118/`；
仓库保留 typed fixture、脱敏指标、失败归属和下一步。完整 Ghost/World Model 目标继续。

## 原始基线与失败归属

冻结 `ac450a20` 后有效矩阵 **6 passed / 6 failed**。六个开启组在四领域都实际维护目标
和邻居；六个关闭组的目标记录在四领域仍全部变化，过期 queue lease 也被回收。触发
scope 控制为 true、周期不取消且所有真实步骤成功；不是服务失败或没有运行维护。
48 个领域对照中，24 个开启对照通过，24 个关闭记录保护均失败。

预检失败保留：首12项在 seed assertion 失败，误把 Affinity 无 scope 公开 listing 当
全局 listing（实际只选 user），尚未进入维护；改为真实 graph.rows() 完整性检查。
第二批3通过/9失败中六个 project 项的 queue fixture 误存 hashed project ref；现有
生产 converter/filter 需要 raw project，而 session 使用 hash。修正编码后原12格完整
复验，原 scorer/断言不变。先前日志分别 `baseline.*`、`baseline-valid.*`，有效批是
`baseline-canonical.*`；不能合并为一次有效矩阵或全记生产失败。

```powershell
python -m pytest tests/test_ghost_maintenance_record_scope.py -q -s --tb=short --basetemp=.e2e-artifacts/ghost-maintenance-record-scope-20261118/baseline-canonical-state --junitxml=.e2e-artifacts/ghost-maintenance-record-scope-20261118/baseline-canonical.xml
```

相关14文件 **431 passed / 2 subtests passed**。随后全仓 Ruff、完整702生产文件
mypy/Pyrefly/ty及precision gate通过；新增fixture三类型检查通过，额外Any审计owned
零诊断，未修改依赖10864项/334文件导致额外审计exit1，独立保留。
完整命令/指标见[脱敏基线](../../tests/manual/ghost_memory_routing/reports/maintenance-record-scope-baseline-20261118.json)。
未改生产或默认开关；下一步必须冻结逐记录保护、确定性事件与相关边界，再接线。

## 修复分轮冻结：queue 租约与 checkpoint

2026-11-18，生产修改前冻结。先闭合 queue 独立消费者，不将其他三个领域的失败隐藏或
重标为通过。复用上述12格内 queue 的6正/6负证据，不重跑模型。主要变量为真实 queue
owner 在写事务内读取实际 inbox 控制并按记录身份选择；沿用既有严格 transition 事件，
replay 不读取当前控制。普通 App/default daemon 已调用该 owner，无新开关。

控制快照须完整校验现有 scoped entries，独立于其他三个控制。queue project 使用 raw
normalized ref，session 使用 session_key；不能把形似hash的原始session当作已编码身份。
user deny 继承至所有记录，显式session/project deny保护该身份。现有queue session没有
canonical父project来源；存在任一project deny时，session记录保守不回收，直到来源证明
另行实现；允许user/其他project仍回收，不猜父子关系。该限制必须在交付中明确。

有scope deny或user deny时，queue全库checkpoint（包括mutation内部自动checkpoint）暂缓，
避免维护被拒绝scope的历史；继续追加允许transition，保留原始历史。不能把行相等称为
选择性compaction。其他显式业务操作、删除权威及retention/purge不纳入本轮修复声明。

验收冻结：新增正常App同12组合queue专属断言（原四领域scorer不改）；补user/legacy及
其他三个控制、raw/hash身份、未知父project、损坏控制fail closed、过期/未来lease、重试
上限、容量满载、禁用/恢复、撤回、删除、冷重启/rebuild、事件/投影故障回滚及事务并发。
每个状态组都检查真实行/事件，允许对照必须实际变更；rebuild切换当前控制后仍须复现
实际历史。阈值预算设低以实际触发checkpoint，restricted组不得生成全库snapshot，
恢复允许后须可正常checkpoint。线程barrier只控制真实事务顺序，不代替生产结果。

停止条件为冻结验收完成或fixture/检查错误；错误归属与原始日志保留，不能放宽scorer。
修复通过相关回归、全仓Ruff/三生产树类型检查/precision及owned额外Any审计后，默认
接入master并独立提交。原四领域矩阵仍作为未闭合总门；后续逐领域继续，不宣称G0完成。

## Queue 分轮结果

[Queue生产修复](queue-record-maintenance-production-20261118.md)新增46项与最终20文件495项
通过，全部指定静态门通过，三个owned文件额外Any零诊断。原12格再次6通过/6失败，
queue子比较已12/12，其余三个领域的关闭组仍失败；原scorer不改，不称全局修复。
普通App/default daemon及直接owner默认消费，有scope限制时暂缓queue全库checkpoint；
窗口可能超预算和未知父project的session保守暂停均明确保留，不算完整G0完成。

## 剩余三个领域修复冻结

2026-11-18，生产基线`e5ca1bf7`，修改前冻结；复用有效12格原始负结果及queue已通过的
实际子比较，不新增模型请求。目标是在同一普通App入口通过**原12格四领域scorer**，
不改断言、身份编码、时间或邻居实际维护门。主变量是owner写锁内的逐记录维护选择与
持久事件；正常post-turn接受新事实不由maintenance开关代替accept_events控制。

复用不可变inbox policy，明确Hebbian/continuity raw、Affinity encoded身份，user deny
继承，未知父project的session按queue相同保守规则暂停。Sleep显式调用维护来源同步；
普通学习来源同步保持原控制职责。每次在写锁内重读，不缓存上一周期允许。

Hebbian decay改为实际允许节点/边的upsert及既有精确pruned事件；不再以全库checkpoint
表示一次衰减。无独立scope的边由两端判定；任一端被拒绝就保留边，并保留其另一端以免
衰减裁剪产生孤边。Affinity还须检查边自身scope。受保护端点不被容量裁剪或来源强化覆盖。

Affinity新增严格的维护boundary事件，持久化实际选择的decay节点/边ID；replay先恢复
当前有界投影，再只衰减这些ID并执行确定性bounds，不读现时policy。维护sync在已接受
来源强化后用空decay选择的同类boundary固定本次有界投影；不能让历史未落盘的中间容量
裁剪跨过维护边界。每个source提案若会裁剪protected行则不接纳该提案，其余允许来源
继续；不能因另一scope deny取消全周期。坏身份/重复/缺失引用/坏时间事件须明确拒绝。

continuity维护只过期eligible行，只消费eligible来源；protected现存行保留原样并预留
容量。追加实际upsert后记录严格的最终成员ID boundary，replay按实际保留集投影，不用
旧synced的全局expiry语义，也不把容量淘汰伪装成过期。显式删除权威优先，不复活retired
sources。三个领域的全库checkpoint在scope限制期间暂缓，不把它称为选择性compaction。

验收：原12格及新增的直接owner/普通Sleep、user/其他独立控制、raw/encoded、未知父scope、
跨scope边/端点、满容量/degree、阈值裁剪、过期/重新来源、撤回/恢复、删除/冷重启/
rebuild、事件与projection实际写后故障回滚、并发deny的两种提交顺序，以及checkpoint
真实超预算暂停/恢复。新事件有效/无效输入都检查；当前控制改变不能重解释历史维护。
若失败先分生产/旧夹具归属，保留原始日志，不改scorer或跳过断言。

停止条件为上述完成或fixture/检查错误；修复后相关回归、指定五项全树门与所有owned
脚本三检查器/额外Any审计通过，再在master默认消费者提交并更新板/roadmap。无新默认
关闭flag。受限消费窗口有界化、旧非canonical身份、混版本及完整pending/retention仍
单列，不能以本轮通过冒称完整G0；随后继续这些真实缺口和有新增预测信息的World Model。
