# Hebbian control payload 重放验证

- 日期：2026-11-18；关联 0.6.3 / 0.6.4。
- 状态：`PRODUCTION_MERGED`（限定三个 Hebbian 控制 payload；本地 master 默认接线）。
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

## 基线结果

冻结提交 `4c5feeb6` 后，168/168 fixture 均证明真实两节点/一条边和预期 producer。
**36 passed / 132 failed**：24 个合法当前/历史格式均成功；scope_deleted 缺 payload
或 scalar payload 的 12 个坏记录已拒绝；其余 132 个坏控制记录两次 replay 都误报成功。
168 项的两次投影与原日志均不变；这是校验漏检，不是观测到状态丢失。
三 scope、warm/cold 一致。另两个旧 Hebbian 文件 100 项通过，合批 136 passed /132 failed。

初次预检因 basetemp 父目录不存在产生 1 个 setup error，建好专用 artifact 目录后，
预检证明 18 个当前格式正常，并在首个坏 candidate_removed 处观测预期生产失败。
这些预检不计入冻结 168 项结果；未更改 scorer。完整基线使用 `baseline-state`，保留
通过与失败的全部 SQLite fixture、XML 和 stdout；原始数据不入库。

```powershell
python -m pytest tests/test_ghost_hebbian_control_replay.py tests/test_ghost_hebbian.py tests/test_ghost_hebbian_replay_integrity.py -q -s --tb=short --basetemp=.e2e-artifacts/ghost-hebbian-control-replay-20261118/baseline-state
```

基线全仓 Ruff、完整 702 文件生产树 mypy/Pyrefly/ty/precision gate 和新测试定向三类型
全部通过。owned Any 零诊断，额外审计 exit 1 来自未修改依赖 3873 项/181 文件，
不增加豁免或改检查范围；Pyrefly 保留既有 35 suppressed /158 warnings。
检查期间生产树与测试 SHA256 冻结验证通过。逐 cell 与精确检查命令见
[基线报告](../../tests/manual/ghost_memory_routing/reports/hebbian-control-replay-baseline-20261118.json)。

## 最小修复与消费者

基线实验独立提交 `9b90e1e7`。生产 owner `GhostHebbianStore` 的原 replay 增加一个
严格控制 payload 解码边界：三种控制精确匹配已证明的 keys，所有 counts 排除 bool、
float、字符串、null、缺失和负数；candidate_id 必须为非空白字符串；scope 必须合法，
user ref 必须为空、project/session ref 必须非空白。schema1 decay 只允许原两字段
或完整四字段，部分新增字段不能伪装成旧格式。无 count 历史总量证明或迁移扩展。

candidate_removed/state_decayed 的前置 snapshot 已包含变化，所以只校验审计标记，
不再执行删除或衰减。scope_deleted 保留原有重放删除语义。失败沿用现有事务返回 False，
保留有效投影与原日志，不跳过、修造或物理删除损坏事件。现有普通 CLI
`python -m codey ghost rebuild-state --state-home <目录> --yes` 直接消费，无新增开关、
模型请求或无人调用模块；这是存储契约修复，不冒充模型收益。

原 168 项未改 scorer，候选修复后全部通过。新增 153 项真实边界也通过：每个 recorded
counter 的六类坏值 48 项、candidate identity 18 项、scope/ref 18 项、半缺新 decay
pair 6 项、三 scope×三 control×合法/损坏×有/无 CLI 确认 36 项、四开关关闭后的
delete/reset 与冷恢复 18 项、事务写入后失败及不读 clock 的恢复 9 项。
每项保留真实 producer；拒绝与恢复检查原始 sequence/event_id/payload，graph rows
同时核验 owner cursor。scope delete 保留坏历史，重建仍拒绝且无复活；reset 推进
有效窗口，随后可成功重建空投影，原始历史前缀均保留。

已有开关继续可关闭自动维护、检索和提示准入；显式 delete/reset/rebuild 仍允许。
隔离时保留日志和最后有效投影，暂停显式 rebuild；回滚可 revert 此生产修复，
无 schema 迁移，但会重新暴露原 132 项漏检。未验证模型行为不受此修复改变。

## 最终验证

原矩阵 **36/168 →168/168**，基线源码 SHA256 完全相同。24 个合法当前/历史格式
两次均成功，144 个坏 payload 两次均拒绝，全部投影与历史不变。新增 153 项包含在
最终 **32 文件 1421 passed /34 subtests** 中，零失败、错误、跳过；不与预检/候选试跑
相加。扩大批覆盖普通 directive/App→HTTP、四开关、来源/候选撤回、过期、真实容量
维护、Sleep、Manifest、删除/reset、重开和事务/进程故障回归。未运行全量 pytest。

全仓 Ruff、完整 702 文件 codey 生产树 mypy/Pyrefly/ty/precision gate、三个本轮
Python 文件定向三类型均通过。额外 Any 审计零 owned 诊断；exit 1 来自未修改依赖
3873 项/181 文件，未改范围或增加豁免。生产 Pyrefly 保留既有 35 suppressed /
158 warnings，定向 Pyrefly 零错误/警告。无 cast、ignore、新豁免或前端变动。
生产树及三个 owned 文件检查期间 SHA256 冻结验证通过；报告中的 owned hashes
是受检工作树字节哈希。命令、逐 cell 和限制见
[生产报告](../../tests/manual/ghost_memory_routing/reports/hebbian-control-replay-production-20261118.json)。

本地保留 baseline/production 两个独立 basetemp、原始 XML/stdout 和检查日志；只将
复现测试、脱敏指标与记录入库，未复制 worktree。回滚及故障隔离见上一节。

## 剩余审查

本轮只闭合三个控制 payload 的结构与身份契约；不证明 counts 与任意历史总量语义相等，
不证明 event identity/envelope 的所有扩展、未知旧事件迁移、全部容量/中间裁剪历史、
所有领域 replay 或跨进程硬退出/混版本恢复。磁盘 retention/purge、其他 family、
provider outcome、repair/Explain 与 World Model 后续真实预测消费者仍未完成。
World Model 重复当前 verification 事实的两次负结果保留，不换提示词重跑或堆默认关闭空壳。
