# Hebbian 节点容量裁剪与跨 scope 删除后的重建

- 日期：2026-11-18；关联 0.6.3 /0.6.4。
- 状态：`INCOMPLETE`（冻结，尚未执行矩阵）。
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
