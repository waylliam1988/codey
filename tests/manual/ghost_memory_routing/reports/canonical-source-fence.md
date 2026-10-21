# Canonical Context Source Commit：来源事务对照

日期：2026-10-21。结论：EXPERIMENT_ONLY，统一生产重构仍为 INCOMPLETE。

42 cell 的正式 producer 为 `d085ef87`，脚本 `e74e75db`、脱敏指标 / 完整性测试 `88707458`。
候选源码位于 `ghost/canonical-context`：`18b7bfa7` / `824c3f34` 来源 fence；`13158952` 顺序折叠。
候选源码尚未合并 master。完整改动、回归命令、成本限制和下一步见
[进度文件](../../../../docs/ghost_progress/canonical-context-source-commit.md)。

## 目标、配置和范围

验证由 canonical 来源生成的摘要，在生成期间发生 append / branch / source edit / 删除重建后，
能否由原有 durable writer 拒绝；区分已有 store CAS 与新增的来源条件。额外验证已经生成的
replacement 能继续被编辑、按同一 SQLite 路径恢复，不重写原始事实。

- 模型：synthetic-source-lifetime，固定 native done，不回显原始事实或 Ghost hint。
- endpoint：每 cell 独立 ephemeral loopback HTTP；不使用 KoboldCpp / live 模型。
- Completions / Responses；context 16384、reserve 2048、keep_recent 1000、output 128、temperature 0，
  timeout 5s、max_turns 1；counter 为 ceil(serialized characters / 4)，method=estimated。
- 2 codecs × 3 fence 臂 × 7 来源条件 = 42 cells，1 rep。每 cell 五个真实任务，再生成 canonical
  摘要、观察来源事务并停止。总计 210 tasks / 210 main HTTP / 231 auxiliary HTTP / 420 task effects。
- 三臂：移除来源条件、仅保留 revision、保留 head + revision。只在实验 wrapper 移除条件，
  不改 production scorer / provider payload / SQLite commit。
- prelude hints on；本测量没有第六任务的 hint on/off 行为得分。该字段必须为 null，不是 0%。
- 3 个 setup cell 另留本地归档，未混入正式分母。原始 HTTP / events / state 留 ignored artifacts。

## 结果

[脱敏指标](canonical-source-fence-metrics.json) 可独立复算。每格分母 2 为两个 codec。

| 来源条件 | 无 fence 接受 | 仅 revision 接受 | head + revision 接受 |
|---|---:|---:|---:|
| 无变化 | 2/2 | 2/2 | 2/2 |
| 摘要后 append | 2/2 | 0/2 | 0/2 |
| 摘要后 branch | 2/2 | 0/2 | 0/2 |
| 摘要后 source edit | 2/2 | 0/2 | 0/2 |
| 摘要后同 ID / revision 删除重建 | 2/2 | 2/2 | 0/2 |
| admission 后 / store commit 前 append | 0/2 | 0/2 | 0/2 |
| admission 后 / store commit 前删除重建 | 0/2 | 0/2 | 0/2 |

只检查 revision 的候选被删除重建反例否定。head + revision 在受测来源变化中拒绝旧提案，
保留健康写入。后两行是原有 store CAS / entry parent chain 的保护，不能归因于本次新增 fence。
所有 canonical 摘要 source 均含合成早期事实、无注入 hint，事务结果与落库条目数量一致。

折叠专项首先复现 7 failed / 6 passed，候选顺序折叠后 17 passed：两后端、两 codec attempt、
嵌套 replacement / omit、原始锚点覆盖、fork cap、branch 与重启。来源 fence 最终 37 passed。
扩大回归 648 passed / 5 既有 skip；mypy 495 files clean、Pyrefly 0 errors / 29 suppressed /
74 warnings、ty clean。不是全仓 pytest，不把既有 skip 计作本阶段安全证明。

## 复现

在 master 上可只复算脱敏结果和运行报告完整性测试：

```powershell
python tests/manual/ghost_memory_routing/canonical_source_fence.py --report tests/manual/ghost_memory_routing/reports/canonical-source-fence-metrics.json
python -m pytest tests/test_ghost_canonical_source_fence_report.py -q
```

完整来源实验必须从候选 checkout 运行。精确重现历史 producer：

```powershell
git worktree add artifacts/ghost_source_commit_repro d085ef87
Set-Location artifacts/ghost_source_commit_repro
python tests/manual/ghost_memory_routing/canonical_source_fence.py --suite canonical_source_fence_repro
```

验证最新候选（包括折叠修复）则 checkout `ghost/canonical-context`；每次 suite 使用新目录，
新运行记录真实 commit，不覆盖历史指标。master 上缺来源 API 时 runner 明确拒绝，不做 fallback。
未调用 live 模型，因此没有 Kobold 故障 / 模型阴性；本结果不替代模型安全与语言质量验收。

## 生产边界和下一步

收益是可复现的来源事务安全与 checkpoint 重建正确性。来源条件不增加模型调用或 schema，
但尚无生产 latency benchmark。实际消费仍是实验 wrapper，不能把未接默认消费者的 API 晋级。
master 的默认 prompt / production writer / exporter / kernel gate 均不变。

接下来接唯一 canonical compaction producer 与 warm 消费；解决 dispatch 前 input 已入库、
后提交 edit 被 before_entry_id 截断的时序约束。范围必须完整绑定，不能直接读未来 edit。
之后才移除额外 exporter、将现有 gate 收敛到实际 attempt；辅助 effect / native pending /
取消和故障恢复 / live 模型安全 / 成本尚未验收。World Model 保持 SHADOW。
