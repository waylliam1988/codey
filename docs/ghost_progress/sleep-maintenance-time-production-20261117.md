# Ghost Sleep 显式维护时间生产接线

- 日期：2026-11-17；关联：0.6.4。
- 原始负结果 commit：`fff79f94d9e27aaf056d4a710c26a4ed1ad27492`。
- 状态：`PRODUCTION_MERGED`（限定普通Sleep维护时间及其实际消费者）。
- 模型/endpoint：不使用；本次验证维护时间与事务边界，不新增模型行为收益主张。

## 冻结目标与实现

保留[原15-cell矩阵](sleep-maintenance-clock-baseline-20261117.md)、恒定重复、真实变化对照、
前跳与后跳时钟，不更改权重/TTL/租约阈值或digest比较。默认普通Sleep入口一次捕获
周期起点，UTC规范化为显式`maintenance_as_of`；传递给Affinity同步与来源检索、
Hebbian/Affinity衰减、continuity候选与合并，以及work queue租约回收。
报告单独保存此逻辑时间，`started_at/finished_at`仍表示实际审计时间。

维护消费者支持显式逻辑时间；独立调用未提供时，只在各自入口取一次当前时间。
显式空值/坏值拒绝，不能借`or now`退回墙上时间。正常ISO offset规范化到UTC，旧naive
输入按UTC。Sleep非法时间在任何步骤写入前失败；各store沿用既有异常/失败返回契约。
Hebbian衰减行事件及control event、continuity刷新事件和work queue transition使用
相同逻辑时间。没有第二份权威日志、schema迁移、新模块、后台模型调用或默认关闭开关。

Affinity来源读取必须使用同一时间筛选work item expiry。为接通这一真实消费者，
同步清理`affinity_sources.py`全文件Any：明确Hebbian/queue/research/ledger领域输入，
使用具体字段而不是动态getattr/_field；terminal failure在Mapping边界校验后读取，
metadata解析收窄容器成员为object。保留既有奖励、scope编码、概念转换、失败去敏与取消传播。
`affinity.py`两处排序lambda改为明确签名，排序键和方向保持不变。

原所有权测试依赖无类型SimpleNamespace及无参数list_items替身，现改用真实领域类型和
真实store事务；provider使用真实RunLedgerProjection/ProviderFailureSummary，仍断言
敏感原文不进入spec。模块内部import的身份断言从明确类型的成员表读取，不为测试新增
生产导出、不放宽检查。其余六个原行为用例均保留。

## 验收与失败归属

第一批候选：**158 passed / 2 subtests passed**，包括原15 cell全部前跳/后跳一致，
以及五个store/Sleep相关既有回归与来源转换所有权验收。

新增`test_ghost_maintenance_as_of.py`共63项：三个scope×六个消费者在禁止墙上取时下
实际完成维护/读取；七入口×五坏时间拒绝且所有领域行与原事件完全不变；UTC/offset/naive
跨三审计时钟运行完整真实Sleep，持久投影与步骤结论一致、重开报告保留逻辑时间；
最小衰减间隔不新增事件；三store写投影失败原子回滚并可重试；取消周期不维护；
旧报告缺字段仍可读且不猜测历史逻辑时间。与原矩阵及所有权测试合计**85 passed**。

首次owned类型预查暴露Affinity旧排序lambda六项Any，已明确签名；随后mypy发现测试
直接访问未显式导出的内部import，已在测试所属边界修正。原失败日志保留，不增加导出或豁免。

扩大42文件生产回归：**1047 passed / 36 subtests passed**，无失败、错误或skip，覆盖
普通Chat/发送、post-turn、四开关、scope、删除/reset、容量窗口、重开/恢复、损坏与取消。
随后补强20项已有时间测试的调用计数，防止来源reader吞掉时钟异常后假通过；
Pyrefly展开警告发现三个旧动态读取清理后多余的str转换，已删除，未改变字符串字段语义。
最终12文件复验：**281 passed / 2 subtests passed**，无失败、错误或skip；包括63项新增验收、
原15-cell矩阵、所有权、五store/Sleep、来源控制、取消与正常post-turn消费者。
不将批次数量相加，也不把1047项称为最终源码在全部42文件的复验。

最终相关回归后串行全树Ruff、生产树mypy/Pyrefly/ty与precision全部通过；Pyrefly为
0 errors、35既有suppressed及158 warnings，与原基线相同。六个生产文件、一个修改旧测试、
一个新增验收脚本及未改基线脚本共九文件，单独通过三类型检查，Pyrefly零警告；额外Any审计
owned零诊断。未改依赖182文件3930项诊断使额外审计exit1，不声称全树Any清零。
没有casts、ignore、新豁免、关闭检查或扩大参数类型后未经验证的动态访问。
所有runtime批次均冻结全702个生产Python文件并复核哈希相同；检查串行，不运行全量pytest。

完整命令、退出码、15项digest/计数和九文件LF源码哈希见
[脱敏报告](../../tests/manual/ghost_memory_routing/reports/sleep-maintenance-time-production-20261117.json)。

```powershell
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
```

## 默认消费者、回退与限制

AppContext现有普通Sleep入口无需新开关即可调用本次默认逻辑；四开关和scope仍由原有控制面
管理。独立decay/sync/reconcile入口也直接使用该时间参数。用户主动关闭、取消、来源退役、
故障隔离及既有delete/reset逻辑保留；实际正常入口及控制回归以本次检查报告为准。

写入失败沿用领域事务回滚和Sleep逐步骤报告，不把未知结果记成成功；原始事件及上一投影保留。
显式非法时间需要调用方修正，不自动猜测历史。可单独revert生产修复，但会恢复已复现的
同周期多次取时缺陷；业务数据恢复应基于原始记录与明确时间。

本实验只冻结相同初始事实、同一逻辑维护时间下的行为。来源在运行期间发生真实变化、其他
scope/身份异常、磁盘retention/purge、不同机器与全领域replay、稳定排序/digest的完整验收
尚未由此证明。continuity过期记录的持久清理/重建还需独立核对，不能从新建TTL一致推断通过。
compaction/snapshot/report中的审计时间与随机ID不参与本矩阵行为digest，未把它们称为逐字节确定。
完整0.6.4、0.6.1–0.6.3与World Model仍未完成。

完整日志、XML、冻结哈希与类型预查保存在本地
`.e2e-artifacts/ghost-sleep-clock-20261117/`；仓库只保留实验脚本、脱敏指标及命令。
下一步继续固定事件的TTL/过期持久投影、稳定排序/digest与其他领域重放，再推进其余
admission/controls边界和真实World Model闭环；不恢复已停止语言实机批次。

2026-11-17 后续：[continuity过期持久事实与消费者](continuity-expiry-production-20261117.md)
已默认闭合continuity过期清理/重建/新队列消费及旧同步日志输入；原18项3→18、旧日志9项
3→9通过，最终1064/4子测试及完整静态门通过。本报告原15-cell测量与历史限制不改，
其余维护入口、稳定排序/digest与全领域replay仍待验收。
