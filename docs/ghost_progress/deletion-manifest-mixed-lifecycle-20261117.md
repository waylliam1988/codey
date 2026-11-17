# Ghost Manifest 不同操作混合生命周期

- 状态：`EXPERIMENT_COMPLETE`（有效负结果）。
- 基线：`28aae81c`。
- 范围：真实应用输入、两个候选 Manifest、旧 writer、失败恢复、rebuild/Sleep/reopen。
- 生产结论：`EXPERIMENT_ONLY`；默认控制面尚未接入 Manifest。

## 冻结矩阵与假设

主要变量是不同 operation 与旧 writer 的交错。A 固定删除 project A，在 inbox mutation/ack
已 commit 后暂停，不持有 SQLite 写锁；B 完整运行后，A 才恢复。B 分别删除同 project A、
不同 project B、共同 session，或 reset_all。旧 writer 先读来源，停在第一次目标写事务前。
失败组让 A 的 continuity 在真实 mutation 后抛 OSError；A 保留 blocked，原 operation
恢复其余 pending 后，新 operation 显式恢复 A 的删除，不抹去 blocked 历史。

32 个主 cell = 四种操作组合 × continuity/work_queue/affinity/sleep 四 writer × 有/无故障。
另有四个 writer-first 对照（同项目、无故障），共 36 cell，各一次。确定性调度不做统计
显著性或模型净收益推断。沿用既有同操作、七 store 故障、spawn 和 schema 证据，不重跑旧矩阵。

每个 cell 先经 AppContext InputQueue、canonical claim/start、确定性 provider、settlement
和真实后台 Sleep 完成三个不同 project 的任务。额外在同一输入原话上显式确认 fixture
偏好，确保各派生 store 有非空真实来源，不伪造 observation 或 canonical receipt。
无模型/HTTP/浏览器，不恢复停止的语言实机批次。

## 有效性与 scorer

有效 cell 必须旧输入三条 observation 存在、四 writer 各有非空 seed、writer 已达写边界、
A 的已提交 inbox 暂停边界及 B 的实际返回均被观测；project 与生产规范化身份一致且 A 的
初始项目记录数必须大于零。故障组须实际注入；pending 以每轮严格减少的方式处理至终态，
不重试 blocked store，新 operation 作为显式恢复。新输入严格在所有调用结束后接收并产生
observation。blocked 是有效负结果，不等于成功删除；completed、rebuild 和新 continuity
属于通过条件，不能混入有效性来筛掉失败结果。
未达到时序、超时或未学习为 invalid，不计作安全通过。

通过条件：旧 writer 在删除后释放必须失败（writer-first 对照成功）；project A 的旧
observation、candidate、active Hebbian、项目 continuity/work item 不复活；B/C observation
按照 B scope 保留或删除，不能误伤未授权 scope。新输入 observation 与项目 continuity 在旧
writer 结束、rebuild、真实 Sleep 和 reopen 后保留；重复已完成 operation 不得再次调用 store。
Manifest/event replay 一致，A blocked 历史不能被后续完成覆盖。全部 cell 无复活/误删才通过。

## 探针修订与早期诊断

首轮在“单次 resume 必须耗尽 pending”断言处停止，未形成完整报告，不能按 36 个 cell
验收。增加逐 cell 本地日志及 `complete=false / expected_cells=36` checkpoint 后定位到
other_project：A/B 的 Hebbian 都出现 `IntegrityError: deleted Ghost graph input`。
每次 resume 原本遇到一个失败即停，因此不能假设只有人工注入的一次故障；改为有界
pending drain，所有额外 blocked 记录进 scorer，完成重试仍禁止调用 store。

第二轮发现 Windows 临时目录返回 `ADMINI~1` 短路径，生产已规范化为长路径，原比较使
fresh continuity 被错误计为零，旧项目也可能被漏计；该不完整报告整体无效，保留在本地
`.e2e-artifacts/ghost-manifest-mixed-20261117/invalid-scope-comparator.json`。探针先 resolve
home，并在有效性中显式校验 scope 身份及非空目标记录；不修改生产路径规范化或放宽
通过条件。另把初稿中 B completed 的要求从有效性移到通过条件，避免将生产 blocked
错误当成无效实验。修订后才执行正式 36-cell 基线，前两轮不计入其样本或收益。

规范化后的两个独立 diagnostic（other_project × continuity × 有/无注入）均有效失败：
A/B Hebbian 均 blocked，project A 仍残留 active 节点，rebuild 失败；fresh observation 和
continuity 正常。真实原因待完整基线确认后修复，不以路径 scorer 修正掩盖该生产缺口。

## 结果

正式基线 36/36 cell 有效，28 passed / 8 failed。失败全部是 `other_project`：四个
writer × 有/无人工故障均失败。A/B 的 Hebbian 删除被 `deleted Ghost graph input`
拦截，新 operation 恢复同样 blocked，project A 仍有一个 active 节点，rebuild 失败。
其余同项目、session、reset 与四个 writer-first 对照均通过。新输入 observation 与
continuity 在全部 cell 保留；没有把额外 blocked 或残留节点算成成功。

根因是 `GhostGraph.replace_rows` 先删除全部节点再插入保留集。A 的 inbox 已退役 A
来源后暂停，B 的项目删除尝试重新插入未变化的 A 节点，被原有退役输入 INSERT guard
拒绝；A 恢复又被 B 的退役来源挡住。这是生产 graph owner 的缺口，不能删除 guard、
扩大删除 scope 或修改 scorer 规避。下一候选应精确保留未变化行，对新建/变化行继续
执行原有写入校验，并验证索引列修复、边与 cursor 的原子性。

复现命令：

```powershell
python -m tests.manual.ghost_memory_routing.manifest_mixed_lifecycle_probe --output tests/manual/ghost_memory_routing/reports/manifest-mixed-lifecycle-baseline-20261117.json
```

脱敏结果：[正式基线报告](../../tests/manual/ghost_memory_routing/reports/manifest-mixed-lifecycle-baseline-20261117.json)。
完整日志及早期无效报告仅保留本地。本实验没有模型调用，不代表模型行为收益。
完整 Manifest retention、升级 receiptless 输入和默认控制面接线不由本矩阵代替。

## 提交前检查

相关 `test_ghost_deletion_manifest.py`、`test_ghost_deletion_manifest_recovery.py`、
`test_ghost_graph_sqlite.py` 回归 50 passed；之后依次全仓 Ruff、完整 `codey` 的 mypy、
Pyrefly、ty 及 kernel precision gate 通过。mypy 覆盖 701 模块；Pyrefly 0 errors、
35 既有 suppressions、159 warnings。本探针另经三个类型检查通过。
额外 mypy `--disallow-any-expr` 本探针零 owned 诊断，未修改传递依赖仍有 351 文件
11157 项诊断、整体 exit 1；不宣称全仓零 Any。日志留本地
`.e2e-artifacts/ghost-manifest-mixed-20261117/probe-any-audit-final.txt`。
`git diff --check` 通过。未运行全量 pytest、停止的语言批次或前端构建。
