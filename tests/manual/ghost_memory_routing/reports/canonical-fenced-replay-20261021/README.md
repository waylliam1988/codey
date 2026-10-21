# Canonical source fence / folding 后的冻结矩阵复跑

EXPERIMENT_ONLY / INCOMPLETE。Producer：`887074582e2262a84e7cf7b171dfa295f5176df3`，
位于 `ghost/canonical-context`。候选源码与 master 隔离；master 仅保存实验脚本、指标与报告。

48 cells / 288 tasks / 288 main HTTP / 160 auxiliary HTTP / 576 已结算 task provider effects。
两个 codec、两个生命周期、宽 / 窄预算、baseline / 删除 export / canonical edit、实际 scoped
hint on/off 均完整。配置与历史冻结矩阵相同，canonical edit 本次真正传入来源 head / revision。
辅助 HTTP 仍不计入 task provider effect 数量；counter 为 estimated，没有 live 模型。

同一 `checkpoint_unification_report.py` 原样校验并复算，观察摘要与原 48-cell 矩阵完全相同。
这是事实保留 / hint 发送 / 调用数量指标的 parity，不声称完整 payload 的字节 parity 或模型安全。
新数据与历史数据独立保存，未混用不同 commit、补缺失控制、调整 scorer 或降低样本数。

canonical edit 在宽 / 窄预算的 reopen / restart 各保留事实 4/4、off 不发送 hint 各 0/2；
每组 on 仍为 2/2。窄预算直接删除 exporter 的两生命周期仍丢早期事实（各 0/4），
baseline reopen 仍重发 hint（2/2）、restart 丢早期事实（0/4）。阴性保留。

在 master 复算：

```powershell
python tests/manual/ghost_memory_routing/checkpoint_unification_report.py --reports tests/manual/ghost_memory_routing/reports/canonical-fenced-replay-20261021
python -m pytest tests/test_ghost_checkpoint_unification_report.py -q
```

从仓库根目录建立精确候选 producer checkout 后重跑；suite 名称可更换，不覆盖已有本地目录：

```powershell
git worktree add artifacts/ghost_checkpoint_fence_repro 88707458
Set-Location artifacts/ghost_checkpoint_fence_repro
$matrixDir = 'artifacts/ghost_memory_routing/replayed-fenced-matrix'
New-Item -ItemType Directory -Path $matrixDir -ErrorAction Stop | Out-Null
foreach ($budget in @('full', 'trim')) {
    $checkpoint = if ($budget -eq 'full') { 'full_replay' } else { 'trimmed_replay' }
    foreach ($arm in @('base', 'drop', 'edit')) {
        $source = switch ($arm) { 'base' { 'baseline' } 'drop' { 'durable_replay' } 'edit' { 'canonical_edit' } }
        python tests/manual/ghost_memory_routing/protocol_source_lifetime.py --suite "fenced_repro_${budget}_${arm}" --lifetime window_reopen app_restart --checkpoint $checkpoint --handoff-source $source --metrics-out "$matrixDir/checkpoint-${budget}-${arm}-metrics.json"
        if ($LASTEXITCODE -ne 0) { throw "Stop incomplete cell/suite; inspect local failure record." }
    }
}
python tests/manual/ghost_memory_routing/checkpoint_unification_report.py --reports $matrixDir
```

完整架构仍未闭环：当前 canonical 摘要由实验提案生成，仍多一次 semantic generation；
warm 视图、读取切点、完整范围绑定、native pending、辅助 effect、唯一实际 attempt gate 与
live 模型成本 / 安全尚未验收。原复算器 `production_gaps` 文本保持不变；部分底层候选修复
不等于缺口已在默认生产消费者关闭。详见
[本阶段进度](../../../../../docs/ghost_progress/canonical-context-source-commit.md)。
