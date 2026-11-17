# 跨 scope 自动维护的记录保护

- 日期：2026-11-18；关联：0.6.2、0.6.4。
- 状态：`EXPERIMENT_COMPLETE`（有效负结果，尚未修复）；生产基线 `9ef0f712`。
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
