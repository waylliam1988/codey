# 图同分裁剪与重建一致性

- 日期：2026-11-18；关联 0.6.4。
- 状态：`PRODUCTION_MERGED`（共享裁剪及受测正常生命周期）。
- 冻结提交：`ff990fb0`。
- 原始负结果提交：`596c2ee0`；生产修复提交：`95fd4e70`（完整 SHA 见生产报告）。
- 基线：`0f36de60`；不重做已闭合的时间/损坏事件矩阵。
- 模型：不调用模型；这是存储确定性实验，不计模型收益或阴性。

## 冻结目标、变量与停止条件

代码审查发现共享节点/边裁剪只按状态、权重和时间排序，同分保留输入顺序；SQLite reader
按语义身份排序，event replay 按历史插入顺序。假设：边达到真实 degree cap 后新增更强边，
普通 Hebbian writer 与同一日志 rebuild 会保留不同的旧同分边。当前尚未声称已复现。

主要变量仅为旧边权重是否并列。不修改生产常量、degree cap、时钟、scorer 或历史。
冻结 `maintenance_as_of` / 事件产生时间为 2026-11-18T00:00:00Z；不增加模型配置。

主矩阵 3 scope（user/project/session）×2 恢复方式（warm/cold）×2 权重组（tie/unique），
共 **12 cell**。每 cell 用实际 inbox ingest/review 和公开 reinforce_candidate 建立 hub、
9 个 peer；按反向语义身份顺序逐个建立 8 个旧边（真实上限 8），再加入第 9 个更强边。
tie 旧边 reward=0.5；unique 旧边 reward=0.1+index×0.05；新边 reward=1.0，confidence=1。
所有节点同 run、不同 slot，scope 正常绑定。比较普通 writer 状态与连续两次 rebuild；
检查事件未变化、8 边上限、新强边保留、节点集合及状态 digest。冷组在 rebuild 前重开 owner。
unique 是正对照；若夹具未建成、scope/来源无效、未达到真实 cap 或重建拒绝，记
`INCOMPLETE_FIXTURE` 并先修夹具，不能算生产负结果。有效状态差异按失败保留，不放宽断言。

辅助契约：三个有效领域节点/边的全部 6 种排列分别裁剪到 2 节点/1 度边，12 项。
主优先级保持既有状态/权重/时间；完全同分以节点 id、边 (source,target,relation) 升序
作确定性 fallback，与 SQLite 顺序一致。未重排语义事件、未合并证据或改变学习/衰减算法。

成功目标：原 24 项全部通过；普通路径默认使用同一稳定裁剪，无新关闭开关。
若仅加身份排序不能闭合实际主矩阵，继续追查 event/live transition，不把纯 helper 通过
当生产修复。生产晋级还须相关正常入口、禁用、scope、撤回、删除、reset、重建、重启及
事务故障恢复回归。故障必须保留有效投影和原历史；不得通过删除原日志获得一致。

## 验证与交付约定

先保存原始失败、有效性检查和脱敏 cell 指标并独立提交，再改生产。修改的 Python 文件
参数、容器、回调和返回类型明确，清理完整文件 Any，不使用 cast/ignore/新豁免。
相关回归后串行运行全仓 Ruff、完整 codey 的 mypy/Pyrefly/ty、kernel precision gate；
本轮文件另做三类型和 Any 审计。不运行全量 pytest，不改前端。

原始 stdout/XML/state 留 `.e2e-artifacts/ghost-graph-tie-replay-20261118`；仓库只保留
可复现测试、脱敏指标和记录。原始基线与生产修复分开 commit，只有正常 master 路径
默认接线后标 `PRODUCTION_MERGED`。同分选择的兼容变化、回滚和限制在结果中明确。
本轮不证明完整 G0、全部容量/排序/digest、跨机器运行或 World Model 生产闭环。

## 原始基线结果

24 cell 全部有效，**9 通过 / 15 失败**。真实 writer 主矩阵 unique 对照 6/6 通过；
tie 6/6 均在第一次 rebuild 后改变状态，第二次 replay 稳定为已经改变的状态，原日志
未被修改。因此不是当前时钟、重启失败或随机事件 ID 导致，而是 DB 语义顺序与原事件
插入顺序在 degree cap 下选择不同的弱边。强边始终保留、节点及 8 边 cap 正常。
辅助节点排列 1/6、边排列 2/6 通过，显示同一有效行集合的裁剪与顺序还依赖输入排列。

复现命令：

```powershell
python -m pytest tests/test_ghost_graph_tie_replay.py -q -s --tb=short
```

相关旧三文件 72 项通过；新测试中的匿名排序回调有一项隐式 Any，已改为具名 typed
回调后复验原 24 项，结果仍 9/15，两批不相加。全仓 Ruff、完整 702 文件生产树的
mypy/Pyrefly/ty/precision gate 及本文件定向三类型检查通过；额外 Any 审计本轮文件
零诊断，未修改依赖仍有诊断（数量和命令见报告），未增加豁免或更改检查范围。
本基线阶段未改生产代码，本结果不是模型阴性。原始日志/XML/state 留本地，
[脱敏报告](../../tests/manual/ghost_memory_routing/reports/graph-tie-replay-baseline-20261118.json)
保留每项 digest、有效性、检查命令及源码哈希。

上述基线之后的修复及边界验收见下；原测量和测试源码保留，不将失败重判为通过。

## 生产修复、消费者和默认状态

`graph_primitives.py` 对既有 rank 先设置语义身份的稳定次序：节点 id 升序，边
(source,target,relation) 升序，然后继续按原状态/权重/时间优先级稳定排序。这样 DB
reader 和 event insertion 的输入次序不再决定同分裁剪。接口使用具体 Protocol，节点
身份与边 relation 明确；边的 active 检查独立于节点 id，保持原有边状态过滤语义。
不改学习率、衰减、过滤阈值、degree/global caps、事件语义或 schema。

本地 master 的普通 Hebbian reinforce/rebuild/decay/compact，以及 Affinity 的
replay/sync/decay/snapshot 已调用同一 helper，修复直接在这些正常路径默认生效。
Sleep、读取和已有消费者继续使用既有 owner，无新增无人调用模块、开关或额外模型请求。
这是不依赖模型的存储确定性修复，不把图一致性或 loopback HTTP 当作模型行为收益。

已有四控制继续可用：自动维护关闭由 AppContext.kick_ghost_sleep 拒绝调度；显式存储
恢复仍允许，delete/reset 在关闭状态仍可执行；retrieve/admit 关闭仍隔离模型提示。
兼容变化：已有同分投影在后续写入、维护或显式重建时，可能改选语义身份较前的记录；
原始事件与来源不物理改写，也不伪造权限、证据或完成事实。没有新 schema 迁移。

## 最终验证与失败归属

原 24 项从 **9/24 → 24/24**；原矩阵文件 SHA256 与原始基线相同。12 个真实主场景
都保持正常 writer 与连续两次 warm/cold replay 的状态相同、原日志不变、强边保留。
辅助节点/边六排列均得到一致顺序和 digest。

新增 30 项：三 scope×8 生命周期（Sleep、真实 5000 event 容量压缩、撤回、scope delete、
reset、过期、App 禁用调度、写入故障回滚及恢复）24 项，加 6 项真实 Affinity event 行的
节点/多 relation 边排序及更强 expired 边过滤。每个生命周期后冷重开并连续重建，
核对状态、cursor 所属 owner、历史前缀和删除后空集；真实 Sleep 产生可查询的 Affinity
directive-order hint。相关 32 文件 **1153 passed / 34 subtests**，零失败、错误、跳过，
含既有普通 App→HTTP、CLI、禁用、scope、来源过期/撤回、删除/reset 和恢复回归。

首轮生命周期 18 通过/6 失败属于测试假设：Affinity 的 Hebbian 来源只映射节点，且
project/session reader 必须传对应上下文；显式 Sleep store 维护与 App 自动调度控制
不是同一入口。已改为精确节点数/边空集/真实 hint 断言和真实 App 禁用调度，未让生产
迁就错误夹具，也未删除行为断言。修正后原 24 个生命周期通过，最终批还包含新增
Affinity 排列；不同阶段不相加。首轮 stdout/XML 与状态路径保留。

全仓 Ruff、完整 702 文件 codey 树 mypy/Pyrefly/ty/precision gate、四个本轮 Python
文件定向三类型均通过。额外 Any 审计 owned 零诊断；exit 1 来自未修改依赖
10864 项/334 文件，本轮 App/Sleep 导入范围比基线大，不直接比较依赖总数作质量结论。
未新增豁免、cast、ignore、关闭检查或改范围；Pyrefly 既有 suppressed/warnings 仍存在。
702 生产文件及四个本轮源码的检查期间哈希冻结验证通过。不运行全量 pytest，无前端改动。

命令、逐 cell 指标和哈希见
[生产报告](../../tests/manual/ghost_memory_routing/reports/graph-tie-replay-production-20261118.json)。
原始基线 12 个 SQLite 数据库另归档至本地 `baseline-state`，共 3424256 bytes，逐文件
SHA256 与保留的失败夹具相同；只复制这些测试数据库，没有复制 worktree。原始日志/XML
与归档清单均在 `.e2e-artifacts/ghost-graph-tie-replay-20261118`，不入库。

回滚：revert 本生产修复，无 schema 回滚；会重新暴露原同分失败。隔离时优先使用已有
run_maintenance/retrieve_projections/admit_hints 控制，保留原日志和最后有效投影。

## 剩余审查

本轮只闭合受测 degree-cap live/replay 与共享身份 tie contract，不证明所有图容量和
历史更新组合、全部领域的排序/digest、跨机器和混版本生命周期。记录型 control payload、
完整 App/硬退出/跨 run 恢复、retention/purge 及 World Model 真实生产消费者仍需继续。
World Model 重复当前事实候选的无增益结论保留，不为完成路线图重跑提示词或堆生产空壳。
