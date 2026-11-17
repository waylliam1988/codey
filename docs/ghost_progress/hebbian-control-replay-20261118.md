# Hebbian control payload 重放验证

- 日期：2026-11-18；关联 0.6.3 / 0.6.4。
- 状态：`INCOMPLETE`（冻结，尚未执行新矩阵）。
- 基线：`cced47da`；不重复已闭合的 envelope、内部时间和同分裁剪实验。
- 模型/endpoint：不适用，不调用模型；本轮不产生模型收益或阴性结论。

## 契约核对与假设

当前 replay 对 scope_deleted 检查 scope/ref，却不检查 counts；candidate_removed 与
state_decayed 仅验证 envelope 后忽略 payload。后二者是 snapshot 之后的审计记录，
不能被当成额外删除/衰减指令。本轮假设：损坏控制记录会被错误报告为有效重建，尽管
最后有效投影可能恰好没有变化；不能将这种错误成功夸大为已观测的数据丢失。

三个现有 producer 分别是 remove_candidate/sync_from_inbox、delete_scope 和 decay。
核对 Git：`f55e5797` 首版的 decay payload 仅 removed_nodes/removed_edges；
`a8050a53` 才增加 decayed_nodes/decayed_edges，两者 schema 都是 1。因此允许确切的
旧两字段或当前四字段 decay 形状；不允许只缺一个新字段。candidate_removed 要求
candidate_id 和两个 removed counts；scope_deleted 要求 scope、scope_ref 与两个
removed counts。计数必须非负 JSON int（排除 bool、float、字符串）；身份非空字符串，
user ref 为空，project/session ref 非空。known payload keys 精确匹配已证实的形状。
不扩展未知事件类型、旧文件迁移、控制权限或 count 与历史总数的语义证明。

## 冻结矩阵和停止条件

唯一主要变量：一个真实已提交 control 的 payload。保留 event_id/sequence/cursor、
时间、其他事件和最后有效投影；损坏注入不是正常 producer 行为。

主矩阵：3 control ×3 scope（user/project/session）×2 owner（warm/cold）×9 variants
= **162 cell**。variants 固定为 valid、missing_payload、scalar_payload、missing_count、
boolean_count、negative_count、string_count、float_count、extra_key。计数故障仅改
removed_nodes；每 cell 普通重建两次。另 3 scope×2 owner 的 legacy 两字段 decay
正对照 **6 cell**；总 168 项。使用真实 inbox ingest/review、reinforce 与上述 producer，
先证明两个节点、一条边及预期控制记录生成，再注入。原时间为
2026-11-18T00:00:00Z，decay 使用明确的 2026-11-19T00:00:00Z；replay 不读取当前时钟。

指标：fixture 有效数；valid/legacy 成功数；坏记录拒绝数；两次返回值、原日志与投影
是否保留；warm/cold 是否一致。valid 和 legacy 必须成功且状态/日志不变；坏 payload
必须拒绝且保留最后有效投影/原日志。不能通过物理删除坏历史、重造 count 或改 scorer
获得通过。夹具未产生真实 control、来源/scope/cursor 异常时先记 INCOMPLETE_FIXTURE，
修正夹具后复验，不将其当生产负结果。若假设不成立保留结果，不强行修改生产。

## 实现与交付门

先保存原始矩阵并独立 commit，再补 owner 解码边界。生产修改复用现有事务与正常
rebuild-state 入口，不新增开关或无人调用模块。补全部字段计数、candidate/scope 身份、
两种合法 decay 形状及非法部分形状的定向回归，并验证 CLI、禁用、scope、删除/reset、
冷恢复、维护和写入故障。record-only control 不能重复删除或重复 decay。

所有本轮 Python 文件清理完整 Any，参数、容器、回调和返回类型明确；无 cast/ignore/
新豁免。相关回归后串行运行全仓 Ruff、完整 codey 的 mypy/Pyrefly/ty、precision gate，
另做本轮文件的三类型与 Any 审计；不运行全量 pytest，无前端改动。

原始 stdout/XML 和隔离 state 留 `.e2e-artifacts/ghost-hebbian-control-replay-20261118`，
pytest 本轮矩阵使用该目录下独立 basetemp 以保留数据库；不复制 worktree。仓库只保留
复现测试、脱敏指标及文档。生产默认接线且检查通过后才标 PRODUCTION_MERGED，记录
兼容边界、回滚及剩余缺口；本轮不证明整个 G0、混版本生命周期或 World Model 完成。
