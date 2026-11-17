# Ghost observation 原始历史与兼容消费视图

- 日期：2026-11-17；候选基线 `c956c5ef`，负结果生产基线 `8cffa97c`。
- 状态：`PRODUCTION_MERGED`（随独立生产修复提交到本地 master）；关联 0.6.3。
- 原始负结果：[旧 reader 基线](legacy-observation-reader-baseline-20261117.md)，15 cell 中 13 通过、2 失败。
- 模型/endpoint：不使用；不恢复已停止语言批次，不主张模型行为收益。

## 冻结目标与实现

目标是在保留全部原始 observation 历史及其身份的同时，阻止已运行的旧 reader
消费窗口外记录，继续允许检索合法新记录；不把容量过期等同用户删除或来源永久退役。

schema9 将既有 observation_events 原地改名为 observation_history，保留行、sequence、
AUTOINCREMENT 高水位、索引、约束和向量外键。旧名成为依赖 observation 窗口与
anchor 的原生只读视图；窗口缺失、未确认或 anchor 缺失时返回空。SQLite 原生拒绝
旧 DELETE/INSERT/UPDATE，包括匹配零行的语句；当前观察写口、窗口、向量和原始审计
使用真实历史表，并保留既有消费准入。

初始化先持有 BEGIN IMMEDIATE，再复查版本并执行改名、DDL、元数据和版本更新；
不使用会隐式提交的 executescript，逐条使用 SQLite complete_statement 执行既有
静态 schema。异常由既有 connection owner 回滚全部迁移。已有 schema9 缺少历史表
或消费视图时拒绝冷启动，不静默制造空历史。

正常 Ghost owner 构造即迁移及启用，无新增模块、实验开关或默认关闭路径。
本次支持的旧消费边界是迁移后新开始/重新执行的查询；已持有的 SQLite 旧快照、
已读入旧进程内存的内容和实际 provider 发送需要独立验证，不能由只读视图追溯撤销。

## 冻结验收与停止条件

保留原 15 cell 的 scope、数据量、公开旧方法、合法新词及最终行为断言；只将
当前夹具的压力灌入与原始审计 SQL 指向真实历史表，旧 reader 方法仍来自真实 Git。
原 42 cell 破坏性旧 writer 矩阵继续验收。现有删除约束测试按新原始表名检查，
旧连接仍使用其真实旧名；不能改成当前 API 替身或跳过旧 SELECT/DELETE。

新增 13 项验收：真实 schema7/8 × 顺序/四线程并发升级共 4 项，保留所有原行、
向量、外键、退役登记并检查 AUTOINCREMENT 下一值；两种旧 schema 各 1 项 DDL 后
故障，要求表名、全部 schema SQL、原行及版本回滚；两种旧 schema 各 1 项缓存
SELECT，要求升级且推进真实 5000 窗口后旧查询返回空、原历史仍在且无额外来源
永久退役；3 种损坏窗口的无 UDF 普通 SQLite 旧视图拒绝；2 种缺失关系的冷启动拒绝。

每项一次，出现失败先归属夹具、旧接口或生产行为，不修改原始 scorer。完成候选
矩阵后停止采样，再运行相关生产回归与完整静态门；生产 Python 在运行中冻结。
不运行全量 pytest、模型或语言批次，不声称其他六领域、物理 purge 或完整 World Model 完成。

## 结果

首轮候选 **266 passed / 1 failed**。原 15 cell 全通过，新增 13 项也通过；唯一失败
是原 42 cell 中 reset_after_upgrade/reset/observations 的旧操作报告 completed。
原历史保留、当前可见性先通过，但首轮候选的 INSTEAD OF DELETE 触发器在空视图上
不执行，不能兑现所有旧破坏性写尝试都拒绝的契约。归属为生产候选问题，不改原断言。

已移除三个 INSTEAD OF 写触发器，使用原生只读视图。新增 insert/update/delete ×
空/非空选择共 6 项验收，要求无 UDF 普通连接也在语句准备/执行时明确拒绝，原历史
不变。原始表的 DELETE 约束继续验收。旧缓存 observation DELETE 的错误从表触发器
IntegrityError 变为 view 的原生 OperationalError，测试精确匹配新拒绝来源；原有
history、rollback 和拒绝断言保留，其他两表及旧读快照 BUSY_SNAPSHOT 归属不变。

第二轮候选 **273 passed**，包含原 15 项、原 42 项、19 项新增验收、原始删除约束、
窗口、receipt、旧投影与经历记忆测试。生产 Python 与本轮扩大回归保持同一冻结版本。
扩大回归 **1122 passed / 136 subtests passed**，无失败、错误或 skip；逐 case 核对原
15 项、42 项和新增 19 项均通过。覆盖实际 CLI/认证 HTTP、Manifest 并发/崩溃/响应
丢失恢复、receipt、来源退役、scope/reset 历史保留、容量维护、向量及各领域消费者和
durable 重启。生产 Python 702 文件哈希在候选第二轮与扩大运行中保持同一冻结版本。
随后串行全树 Ruff、mypy（702 个生产模块）、Pyrefly、ty 和 precision 全部通过；
Pyrefly 为 0 errors、35 项既有 suppressed、158 warnings。16 个 owned Python 文件
另经三种类型检查通过，额外 Any 审计零诊断；未修改依赖 351 文件 11596 项诊断使
扩展命令 exit 1，不是全树 Any 审计通过。经历记忆文件的既有动态类型已清理，无
cast、ignore、新豁免或弱化检查。首轮类型检查中 signature(object) 的参数不兼容已
改为具体、可静态赋值检查的 factory Callable；在全部 273 项及扩大批次之前完成。
生产与测试源在最终检查之后没有行为修改。

原始 13/2、候选 266/1、273 和扩大批次分别保留，不相加。最终日志为 production-*，
首轮及修正候选为 candidate.* 与 candidate-v2.*；初始类型诊断也保留在本地。
[脱敏逐 case 报告](../../tests/manual/ghost_memory_routing/reports/observation-consumption-view-production-20261117.json)
保存原 15/42 项、新 19 项、批次结果、实际检查参数、范围限制及 LF 规范化源码摘要。

## 夹具与类型边界

所有原始历史断言仍检查原行、原序号和字段，SQL 表名更新为 observation_history；
共用历史 helper 按实际表存在性兼容真实旧 Git 代码。schema6/7 的历史夹具继续验证
其缺少的来源元数据，不把当前 view 的只读错误当作 receipt 检查通过。

经历记忆模块的容量夹具需要向真实历史表灌入数据，因本轮修改同时清理其原有动态类型：
以明确的 settlement deps、RunFrame/RunWork、类型化 outcome 事件及具体 AppContext
子类替代 SimpleNamespace。该组无 canonical task 的隔离结算夹具使用独立 observation
owner，保留旧夹具的含义；没有为其放宽生产 receipt。显示顺序、用户可见回复而非内部
摘要、无显示时回退、失败仍回答并告警，以及真实容量成本的断言保留。

## 复现命令

```powershell
python -m pytest tests/test_ghost_legacy_observation_reader.py tests/test_ghost_observation_consumption_view.py tests/test_ghost_mixed_schema_history.py tests/test_ghost_history_delete_guards.py tests/test_ghost_event_windows.py tests/test_experience_memory.py tests/test_ghost_canonical_input_receipts.py tests/test_ghost_legacy_projection_admission.py -q --tb=short --junitxml=.e2e-artifacts/ghost-observation-view-20261117/candidate-v2.xml
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
```

扩大回归沿用原事件删除防护的全部受影响测试，加上本次 reader 与迁移验收；实际
完整参数另保存在脱敏报告，不运行全量 pytest。三个类型检查另覆盖本轮全部 16 个
修改/新增 Python 文件，并额外执行 mypy --disallow-any-expr 审计，按 owned 与未改
依赖分别归属。完整日志、JUnit 和失败 state 只留本地 .e2e-artifacts 及 pytest 保留目录。

## 回滚与剩余范围

回滚优先隔离故障消费者并保留数据库、原始历史和永久来源退役，使用支持 schema9 的
runtime 恢复；不能把 user_version 降回旧值、删除历史或把旧只读 view 改回可写表来
掩盖问题。原始磁盘历史仍未有界，显式 retention/purge、其他六领域的旧 replay/reader、
旧快照/内存及整个混版本 App/真实发送仍需后续验证；不宣布完整 0.6.3 或 World Model 完成。
