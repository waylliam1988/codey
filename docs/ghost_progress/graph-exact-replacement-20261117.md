# Ghost 图精确替换与混合删除复验

- 状态：`PRODUCTION_MERGED`（限定 graph 精确替换修复，随本记录提交）。
- 比较基线：`f4102a22` 保存的[36-cell 混合生命周期负结果](deletion-manifest-mixed-lifecycle-20261117.md)。
- 范围：生产 `GhostGraph.replace_rows`；Hebbian 与 Affinity 共同使用。
- Manifest 默认控制面调度尚未接入，本次不代替完整 0.6.3。

## 冻结假设及实现

不同项目删除的 inbox 与 graph 阶段交错时，保留的未变化节点不能被整体重插。
替换后仍须精确等于目标集合，不能以跳过整个写入或顺手删除其他 scope 获得通过。

替换在既有事务内读取节点全部索引列及编码 payload；只有全部相等才保留原行。
缺失节点删除，新建及变化节点使用 DELETE/INSERT，沿用退役 candidate 的 INSERT
guard；不使用 UPDATE 绕过。重复节点仍报 IntegrityError。边集合精确重写，新增或
变化边复查端点是否携带退役 candidate，防止保留节点后从边写入绕过原有输入保护。
原有不变边可保留其含义，不能扩大 scope。事件、行和 consumer cursor 继续同事务。

冻结候选 graph_store.py SHA256：
`d1242f4c5258b77c58ee07b299c31bdc7cee28d2d2c5cacf39321dae90f43f27`。
真实 runtime / spawn 验收期间不修改生产 Python。

## 验收计划

- 新增 23 项 typed graph 回归：两个 graph 的退役但未变化节点保留、精确移除、reopen，
  变化节点/新 alias/变化或新增边拒绝并回滚事件，索引列损坏修复，重复节点、悬空边及
  退役行索引修复失败回滚，另有真实 inbox/Hebbian 不同项目删除与 rebuild。
- 同一探针、同一 scorer、同一 36-cell 矩阵复验到独立候选报告，不替换负结果。
- 受影响的 graph/store、Manifest 原子性/进程/恢复、输入寿命/fence、控制面、Sleep
  及 post-turn 回归；不跑全量 pytest 或新增语言实机批次。
- 相关回归后依次完整 Ruff、mypy、Pyrefly、ty、kernel precision gate；本轮测试另经
  三个类型检查，两个修改 Python 文件额外 `--disallow-any-expr` 审计。

## 结果、生产接线与限制

相关生产回归 **675 passed / 141 subtests passed**，包含本轮新增 23 项；不是全量 pytest。
命令沿用[原子性候选的相关批次](deletion-manifest-atomic-mutation-20261117.md#最终验证)，
额外加入 `tests/test_ghost_graph_exact_replacement.py`，JUnit 与完整命令/日志留本地
`.e2e-artifacts/ghost-manifest-mixed-20261117/exact-graph-regression.xml` 及同名 `.log`。
生产源码固定后运行，未重复相加首轮 23 项结果。

之后依次完整 Ruff、mypy（701 模块）、Pyrefly、ty、kernel precision gate 全部通过；
Pyrefly 0 errors / 35 既有 suppressions / 159 warnings。本轮新测试及 graph 文件另经
三个类型检查通过。额外 mypy `--disallow-any-expr` 两个文件均零 owned 诊断；传递依赖
仍有 193 个未修改文件 4087 项诊断，整体 exit 1，不宣称全仓零 Any。日志留本地
`exact-graph-any-audit.txt`。没有 cast、type ignore、豁免或配置放宽，`git diff --check` 通过。

同一 36-cell 矩阵 **36/36 全有效且全部通过**，比较基线为 28 passed / 8 failed。
四个不同项目 writer × 有/无注入全部闭合；原始负报告保留。删除后的旧 writer 被拒绝，
writer-first 对照成功；新输入、未授权 scope、blocked 历史保留，重复 completed 操作零 mutation，
rebuild、Sleep 与 reopen 通过。脱敏[候选报告](../../tests/manual/ghost_memory_routing/reports/manifest-mixed-lifecycle-exact-graph-20261117.json)
由原探针直接生成，scorer 未改，不以回归数量或模型外推代替本矩阵。

生产 graph 方法已有真实调用者，本修复随正常操作默认生效，
无需新增开关；显式 scope/用户关闭、退役来源、generation 与 canonical receipt 检查保留。
回滚为回退修复提交，无 schema 变更；回退会恢复已确认的混合删除失败。
完整 Manifest retention、升级 receiptless 输入、正常控制面调度、确定性时间 replay
与 World Model 仍需继续，不因单项 graph 修复自动标记完成。
