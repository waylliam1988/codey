# Ghost 实验：Canonical Context Source Commit

- 实验名称：Canonical Context Source Commit / Checkpoint Folding
- 状态：EXPERIMENT_COMPLETE（限定来源事务对照）；统一生产重构仍为 INCOMPLETE
- 负责人：Codex
- 日期：2026-10-21
- 关联 Roadmap 验收门：0.6.1 / 来源、恢复、撤回
- 实验 commit：`e74e75db`（42-cell runner / 复算器）、`88707458`（脱敏指标、完整性测试）。
- master 实验归档 commit：`3d928e51` / `87e92b5c`；与候选源码提交分开。
- 候选源码 commit：`18b7bfa7`（revision fence）、`824c3f34`（head 身份 / 删除重建）、
  `13158952`（按路径顺序折叠替换）；必要测试 `c2583adb` / `d085ef87` / `1a6116f9`。
- 正式 42-cell producer：`d085ef87`；候选分支：`ghost/canonical-context`。
- 生产合并结论：EXPERIMENT_ONLY；上述候选源码不进入 master。

## 实验目标和假设

继续用户授权的统一重构：durable SQLite 持有事实和已提交范围变换，provider 只生成可重建
协议视图；Ghost 自持来源和撤回约束，最终一个实际 attempt gate。先验证来源提交与折叠，
不将受测的事实保留 / 零 hint 重发推广为生产安全证明。

假设一：摘要生成前读取的会话 revision 应当约束摘要提交，不能在生成后读取最新 revision
来认可旧来源。假设二：revision 仍不足以绑定来源生命周期，同 ID 删除重建可能复用 revision。
假设三：一次摘要本身也是视图，后续 checkpoint 对其 replace / omit 必须按提交顺序生效。

## 本次完成

候选沿用 `DurableSession.admit_write`、已有 `DurableMutation.expected_revision`、store CAS 和
entry parent chain，不增加 Ghost admission、writer、journal、数据库或 schema。

新增 `expected_revision` 与 `expected_head` 来源条件。head 条件必须伴随 revision；revision
必须是非负整数，拒绝 bool / 字符串；带来源条件的写入必须立即 placed，不能转为排队写入。
校验进入请求指纹，先检查幂等已提交结果，再比较当前来源；原请求可以在后续写入后安全重试。
admission 与实际 store commit 之间仍由既有 CAS / parent chain 原子拒绝竞态，不去掉条件重试。

最初 19 项 revision 测试通过后，扩大审计复现了 ABA：删除会话、同 ID 重建并产生相同 revision，
旧摘要会被接受为无效 context_edit。候选补齐 head 身份检查；检查后再重建的窗口由 store
原有 parent chain 拒绝。已删除来源不会被派生写入重新创建。

快照原来先收集“原始 ID → 最后一个 edit”，忽略后续针对虚拟 replacement ID 的修改。
新增测试红灯为 7 failed / 6 passed：二次摘要替换、摘要 omit、子 fork 和重启复现失败。
候选改为沿分支路径逐次折叠，每个原始条目保持位置与替换树；编辑原始锚点覆盖整棵替换树。
原始 user / assistant / 工具事实均不改写，不引入另一份持久历史。重启只从同一 SQLite 路径重建。
测试 setup 曾误用 DurableSession 为 context manager，已改用 closing 后再确认上述 7 项真实红灯；
这个 fixture 错误没有算作生产行为阴性。

## 实验配置

- 模型 / suite / endpoint：synthetic-source-lifetime / canonical_source_fence_20261021 /
  ephemeral loopback HTTP。未调用 KoboldCpp。
- 两 codec：Completions / Responses，native done；context 16384 / reserve 2048 / keep_recent 1000，
  output 128 / temperature 0 / timeout 5s / max_turns 1。
- counter：ceil(serialized characters / 4)，明确为 estimated，不冒充 tokenizer。
- tasks / cells / reps：5 个实际 task prelude / 42 cells / 每 cell 1 次；210 tasks、210 main HTTP、
  231 auxiliary HTTP、420 已结算 task provider effects。辅助 HTTP 不计入这 420 项 effect。
- on/off 对照：来源 fence 关闭 / 仅 revision / head + revision；prelude hint 始终开启。
- 七种来源条件：无变化、摘要后 append / branch / edit / 同 revision 删除重建、store commit
  之前 append / 删除重建。故障注入只作用于实验；所有臂都先经过实际 task / builder /
  admission / recorded provider / HTTP / settlement，canonical 摘要真实走辅助 HTTP。
- 每 cell 在来源事务观察结束时停止，第六任务未运行。final hint 结果必须是 null；
  没有把缺失的发送行为对照计作 0%。此前 3 项 setup 不纳入 42 项正式矩阵。

## live / offline 实验报告

详见 [来源事务报告](../../tests/manual/ghost_memory_routing/reports/canonical-source-fence.md) 和
[脱敏指标](../../tests/manual/ghost_memory_routing/reports/canonical-source-fence-metrics.json)。

| 来源变化 | 无 fence 接受 | 仅 revision 接受 | head + revision 接受 |
|---|---:|---:|---:|
| 无变化 | 2/2 | 2/2 | 2/2 |
| append | 2/2 | 0/2 | 0/2 |
| branch | 2/2 | 0/2 | 0/2 |
| source edit | 2/2 | 0/2 | 0/2 |
| 同 revision 删除重建 | 2/2 | 2/2 | 0/2 |
| store commit 前 append | 0/2 | 0/2 | 0/2 |
| store commit 前删除重建 | 0/2 | 0/2 | 0/2 |

后两项无 fence 也拒绝，来自已有 store CAS / parent chain，不能归功于新增来源条件。
仅 revision 的统一候选已被删除重建对照否定。head + revision 的受测 12 项来源变化均拒绝，
健康对照 2/2 接受；不代表任意并发或模型语义安全已被穷尽。

候选 `88707458` 另完整复跑原 48-cell / 288-task checkpoint 三臂矩阵，仍为 288 main HTTP /
160 auxiliary HTTP / 576 task effects；同一冻结复算器原样运行，观察摘要与历史结果相同。
宽 / 窄 canonical 臂的两生命周期各保留事实 4/4、off hint 各 0/2、on 各 2/2。
仅删 export 的窄预算失忆仍复现。新指标独立保存在
[冻结矩阵复跑目录](../../tests/manual/ghost_memory_routing/reports/canonical-fenced-replay-20261021/README.md)，
不与历史 48 项或来源事务 42 项混池；复现命令见该目录，不改 scorer / 样本数。

## 验证命令和结果

候选代码复现需 checkout 候选分支，不能在 master 上把实验脚本报缺能力改成兼容 fallback。

```powershell
git worktree add artifacts/ghost_context_review ghost/canonical-context
Set-Location artifacts/ghost_context_review
python -m pytest tests/durable/test_context_edit_revision_fence.py tests/durable/test_context_edit_folding.py tests/test_ghost_canonical_source_fence_report.py -q --tb=short
python tests/manual/ghost_memory_routing/canonical_source_fence.py --suite canonical_source_fence_repro
python tests/manual/ghost_memory_routing/canonical_source_fence.py --report tests/manual/ghost_memory_routing/reports/canonical-source-fence-metrics.json
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
```

精确 42-cell 生产者可改用 `git worktree add artifacts/ghost_source_commit_repro d085ef87`，其源码已有
head / revision fence，尚未应用顺序折叠；那一版的 folding 红灯测试是保留的负结果，不宣称全绿。
完整矩阵期间没有来源条件外的 provider payload 改动。新候选运行应记录自身实际 commit，
不伪装成历史 producer。

已验证来源 fence 37 passed；折叠 17 passed（含两后端、fork、重启和两 codec 实际 attempt payload）；
报告破坏证据 9 项已通过。扩大测试命令如下，结果 648 passed、5 个既有 skip，130.36s。
最后新增的两个“删除来源不复活”用例由上述 37 项 focused run 另行覆盖，未重跑无变化的扩大集。

```powershell
python -m pytest tests/durable tests/test_ghost_context_admission.py tests/test_ghost_provider_send_boundary.py tests/test_ghost_control_contract.py tests/test_ghost_controls.py tests/test_ghost_checkpoint_unification_report.py tests/test_ghost_protocol_source_lifetime_report.py tests/test_ghost_canonical_source_fence_report.py tests/test_conversation_resume_reuses_bound_window.py tests/test_protocol_window_persistence.py tests/test_context_compaction_transactions_and_lifecycle.py tests/test_context_compaction_within_long_tool_turn.py tests/test_compaction_source_encoding_and_summary_contract.py tests/test_compaction_source_reduction_and_packing.py tests/test_compaction_counted_tool_views.py tests/test_compaction_request_token_accounting.py tests/test_responses_compaction_preserves_complete_exchanges.py tests/test_zen_compaction_auxiliary_request_boundary.py tests/test_native_boundary_message_range.py tests/test_api_fresh_transport_recovers_facts_without_old_calls.py -q --tb=short
```

5 个 skip 为已有的 live-owner / scheduler fixture teardown 隔离，不新增 skip，也不算覆盖完成。
mypy：495 个文件无错误；Pyrefly：0 errors、29 suppressed、74 warnings；ty：通过。
Ruff / diff 检查通过。未运行全仓 pytest，未改 scorer / 门槛 / 历史指标。

## 当前生产边界

- 是否经过真实 provider-send：42 cell 的真实 task HTTP 和 canonical 辅助 HTTP；不是 live 模型。
  折叠回归另走两 codec 的实际 attempt 构造和 HTTP transport hook。
- 是否改变默认 prompt：master 无变化。候选只修改来源事务和已提交变换解释，未移动 Ghost gate。
- 支持模型 / 任务 / 配置：上述确定性 native task / SQLite 消费场景，不推广模型语义质量。
- 成本 / 延迟：source fence 复用已读 head 与原有事务，不增加模型请求或 schema；未做生产延迟
  benchmark。canonical 实验仍有旧 compaction 加额外摘要，不能据此宣称成本已收敛。
- 回滚：候选分支隔离，master 不 cherry-pick 候选源码；原始事实未改写。协议内存仍可丢弃重建。
- 生产合并结论：EXPERIMENT_ONLY。World Model 继续 SHADOW。

## 未完成或阻塞

还没有默认生产 compaction 消费者读取 canonical 来源、提交范围变换并刷新 warm 视图。
不能把上述 writer API 与折叠修复当成整条生产重构已完成，也不能立即删除 exporter。

另一个顺序边界仍需解决：task input 通常在 dispatch 前已入库；若此后才追加 context_edit，
现有 `before_entry_id` 截断会同时排除该 edit。不能盲目把截断后的所有 edit 加回去，
否则跨过当前输入的摘要可能把未来内容带进来。下一阶段需绑定完整来源范围与读取切点。

该切点负结果可在任一 checkout 用以下 0 HTTP 合成 probe 复现（本阶段已运行）：

```powershell
@'
from codey.durable import DurableSession, MemoryDurableStore
s = DurableSession(MemoryDurableStore())
s.create_conversation('cut')
old = s.append_message('cut', 'user_message', {'text': 'raw earlier fact'})
current = s.append_message('cut', 'user_message', {'text': 'current input'})
s.admit_write(conversation_id='cut', request_id='summary', kind='context_edit', data={}, display_only=False,
              edits=[{'target': old.entry_id, 'action': 'replace', 'messages': [{'role': 'assistant', 'content': 'folded earlier fact'}]}])
full = s.context_snapshot('cut', token_budget=100)
cut = s.context_snapshot('cut', token_budget=100, before_entry_id=current.entry_id)
assert full.messages[0]['content'] == 'folded earlier fact'
assert cut.messages == ({'role': 'user', 'content': 'raw earlier fact'},)
print({'full_uses_summary': True, 'cut_uses_summary': False, 'http': 0})
s.close()
'@ | python -
```

来源 IDs / digest 当前仍由实验提案记录；通用 fence 本身不替调用方证明源摘要语义、
目标范围有效性、模型输出未继承 Ghost、权限或 evidence 真实性。尚缺 native pending delivery、
辅助 effect 结算、取消 / 崩溃恢复、唯一语义 producer、最终 attempt gate 和 live 安全 / 成本验收。

## 下一步

用既有 durable 事务为唯一 canonical compaction producer 绑定明确范围与提交切点，
让 cold 与 warm 消费同一已提交表示；先验证失败不写、分支 / 删除不复活、native delivery 不变，
再删 conversation_plan 的额外 export 拼接，最后移动现有 Ghost admission 到唯一实际 attempt。
完整关闭这些缺口前，来源 API 和折叠候选留在隔离分支。
