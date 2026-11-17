# Hebbian 节点容量裁剪与跨 scope 删除后的重建

- 日期：2026-11-18；关联 0.6.3 /0.6.4。
- 状态：`EXPERIMENT_COMPLETE`（真实裁剪后重建差异已确认，生产修复待完成）。
- 代码基线：`bc2534e7`。上一轮控制 payload 修复保留，不重复同分/内部时间/坏控制矩阵。
- 模型、endpoint、采样参数：不适用，无模型调用，不产生模型收益或实验阴性结论。

## 审查疑点和目标

正常 reinforce 每次对 live 节点与边执行 bounds，事件只写 changed rows；replay 却在
读完所有 events 后才执行 bounds。scope_deleted 是追加事件，未自动生成新快照。
因此怀疑：达到真实 500 节点上限后被裁掉的另一 scope 节点，可能在删除强节点所属
scope 后被 replay 再次带回。尚未复现，不把疑点记为生产失败。

目标是验证同一有效已提交日志在正常 writer、scope 删除和 warm/cold rebuild 之间
保持完全相同投影。容量排除不等于用户删除；即使疑点成立，也不能表述为本次已删除
scope 数据复活。删除 scope 必须仍为空，保留 scope 的已裁节点不得在重建时无新输入
地恢复。已有 event 数量预算的历史保留测试不覆盖这个 live node cap 组合。

## 冻结矩阵

12 cell =3 deleted scope（user/project/session）×2 owner（warm/cold）×2 load。
保留 scope 固定轮转：删 user 留 project；删 project 留 session；删 session 留 user。
load=at_cap：先创建 499 个独立保留节点，再添加 1 个更强删除 scope 节点；
load=over_cap：先创建 500 个独立保留节点，再添加同样 1 个更强节点。
不得降低或 monkeypatch 真实 MAX_GHOST_NODES=500；不生成边，避免混入 degree 变量。

每个节点使用真实 inbox ingest→明确 accept→public reinforce，不直写伪造图行。
保留节点 reward=0.5，强节点 reward=1.0，confidence=1.0，独立 conflict/value keys。
时间为 2026-11-18T00:00:00Z+i 秒；每次接受后立即强化，避免 inbox 真实 200 项窗口
裁掉尚未 review 的输入。旧接受项在 inbox 因容量离开是正常已知边界，不运行 sync
重新导入，也不把这些输入重新伪装成当前 inbox 行。所有运行不读取真实时钟。

先证明每个真实写入成功、保留节点数为 499/500，添加强节点后总数恰为 500，强节点
存在，保留 scope 恰有 499 项；over_cap 排除 1 个保留身份，at_cap 排除 0 个。
然后调用现有 public delete_scope，证明只移除强节点（1 node/0 edge），生成合法
scope_deleted，保留 scope 仍有 499 项，删除 scope 为零。
cold 组在此重开 owner。普通 rebuild 连续两次，不读 clock，不插入或修造历史。

主要变量只为真实 node-cap pressure，scopes/owner 是边界分层。主要指标：fixture
有效数、两次重建成功数、完整图投影一致数、容量排除身份再现数、删除 scope 可见数、
原始 sequence/event_id/payload 前缀与 cursor 保持情况。成功标准：12 项均重建成功，
两次投影与删除后 live 状态相同，排除身份未出现，删除 scope 为空、原历史未改。
任一 fixture 未完成真实接受/裁剪/删除先标 INCOMPLETE_FIXTURE，修夹具而非改 scorer。
假设不成立则保留结果，不为补路线图强行修改生产。

## 交付与生产门

先独立提交原始矩阵/脱敏指标，再决定 owner 修复。若真实问题确认，优先在产生裁剪的
正常写入中记录可重放投影边界，复用现有事务/window，不新建中央日志或默认关闭开关。
不能仅在测试入口或 CLI 事后加修补，也不能删除历史、永久退役容量裁剪身份，或改变
真实容量预算来获得通过。旧日志缺失裁剪边界的可恢复范围必须明确，不能虚构历史。

生产候选需要补实际 reader/CLI、scope/delete/reset、关闭、撤回、Sleep/过期、原子故障
及 cold 恢复，特别是节点/边裁剪、两条 reinforce 分支和无裁剪路径；按实际实现选择
必要回归。不要求重跑所有历史实机实验，不运行全量 pytest。
本轮 Python 文件完整 Any 清理，无 cast/ignore/豁免；相关回归后跑全仓 Ruff、完整
codey 的 mypy/Pyrefly/ty/precision gate，以及 owned 三类型与 Any 审计。实现和受检
源码哈希冻结。每个独立实验、生产接入同步 board/roadmap/记录并 commit。

原始 stdout/XML/SQLite 和完整检查日志保留
`.e2e-artifacts/ghost-graph-capacity-scope-replay-20261118`，使用独立 basetemp；
仓库只留复现脚本、脱敏指标和必要 fixture。不复制 worktree。
本轮不证明全部图历史、其他领域、完整 G0 或 World Model；World Model 仍需有新增
预测信息的真实消费者，不重跑重复当前 verification 事实的无收益候选。

## 冻结基线结果

冻结提交 `50b58c39` 后，**12/12 fixture 全有效，6 passed /6 failed**。
at_cap 三 scope×warm/cold 六项均保持删除后 499 个保留节点；over_cap 六项在两次
普通成功重建后均变为 500 个节点，原本因容量排除的同一身份两次均重新出现。
全部原始 sequence/event_id/payload 不变，删除 scope 可见数始终为零；这不是被删
scope 数据复活，也不是来源重新学习。第一次重建改变了有效投影，第二次保持错误集合。

已有图 tie 24 项和 control payload 168 项继续通过，合批 **198 passed /6 failed**，
用时 812.96s；不运行全量 pytest。预检首项通过后结束预检进程，第二项夹具构造中断，
不计入冻结矩阵，不算生产失败；其日志/局部 state 保留。没有改变源文件或 scorer。

全仓 Ruff、完整 702 文件 codey 的 mypy/Pyrefly/ty/precision gate、新测试定向三类型
均通过；owned Any 零诊断，额外审计 exit 1 来自未修改依赖 3873 项/181 文件。
生产 Pyrefly 保留既有 35 suppressed /158 warnings，定向零错误/警告，无新增豁免。
检查期间 702 文件与新测试哈希冻结通过。

逐 cell、命令和哈希见
[基线报告](../../tests/manual/ghost_memory_routing/reports/graph-capacity-scope-replay-baseline-20261118.json)。
完整日志/XML 与全部通过/失败 SQLite 只留 `baseline-state`。这些 DB 是实验后状态，
over_cap 投影已被旧 replay 改成 500，不能拿它冒充最初的 499 快照；实验前 digest、
排除身份与两次结果留在指标中。未承诺自动恢复此前已被旧 replay 覆盖的投影。

```powershell
python -m pytest tests/test_ghost_graph_capacity_scope_replay.py tests/test_ghost_graph_tie_replay.py tests/test_ghost_hebbian_control_replay.py -q -s --tb=short --basetemp=.e2e-artifacts/ghost-graph-capacity-scope-replay-20261118/baseline-state
```

## 生产候选与追加边界（待实现）

根因是 live reinforce 已裁剪，但日志没有表达这个 writer 边界。优先用同一 graph
事务记录**实际移除的节点/边差量**，使两条 reinforce 分支共享同一提交逻辑；不在每次
单节点裁剪后复制完整 500 节点快照，避免原历史永久保留时的磁盘放大。无裁剪不得新增
事件；裁剪记录大小应随实际移除数量变化，不能靠新增静默中央索引或强制退役身份。
维护仍沿用已有 snapshot/window，普通 rebuild-state 消费新事件并默认生效。

新裁剪事件必须有独立严格解码，不混同 snapshot 后 record-only 标记。旧日志没有
中间裁剪事实时，不能伪造确定历史；删除控制前存在未记录 bounds 的歧义应拒绝重建并
保留当前投影/原日志，不能自动把旧 500 修称为原 499。保持可显式 reset/合法维护恢复。
这是后续候选契约，尚未产生生产实现或通过结论。

追加检查目标：真实 node/degree 裁剪、两个 reinforce 分支、无裁剪无新增事件、记录
大小随 delta 而非全图增长；坏 payload 原子拒绝、旧缺边界日志保护、正常 CLI/reader、
四开关、跨 scope 删除/reset、撤回、Sleep/过期、写入故障与冷恢复。新格式对旧 reader
及 rollback 的限制要如实记录，不能声称任意混版本兼容。原 12 项源码不改；再跑必要
回归和要求的全部静态门，收益与生产边界通过后独立提交生产，而非只留默认关闭模块。

### 候选提交身份追加回归（实现中冻结）

候选一已实现差量记录，40 个小型边界试跑通过；统一验收尚未完成时，静态核对发现
control_event 默认不分配 event_id，GhostGraph 会以整条 payload hash 去重。固定同秒
重复裁剪可能丢失真实转换；原 edge/node upsert 在重学相同 payload 时也可能被去重。
因此主动结束候选一的统一 pytest，记 INCOMPLETE，不记整个矩阵通过或生产阴性。
保持受检源码不变，等待该冻结检查批完成，再验证下面追加的候选回归。

追加 3 cell：三 deleted scope 及既定保留 scope 轮转，时间固定 NOW。用现有真实 8 degree
cap 夹具（hub+9 peers），每次从当前缺边的唯一 peer 重新强化 reward=1，共 9 次，
覆盖同秒裁剪身份重复与同 payload 重学。然后创建/删除另一 scope 的独立节点，普通
rebuild 两次必须成功且与 live 8 边完全相同、原始历史不变。先验证候选一，记录实际
失败/成功；若身份去重问题确认，真实发生的强化事件与裁剪事实均需每事务独立身份，
无变化的 duplicate_evidence 仍不写。此回归不修改原 12 项 scorer 或扩大模型结论。
