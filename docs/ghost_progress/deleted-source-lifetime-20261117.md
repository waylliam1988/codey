# Ghost 删除后重新读取来源的生命周期

- 日期：2026-11-17
- 基线：`7eded52e`
- 实验状态：`EXPERIMENT_COMPLETE`（限定来源重新消费负结果）
- 生产结论：`EXPERIMENT_ONLY`（修复单独提交）
- 模型 / endpoint：无，不恢复已停止的语言批次。

## 固定假设和矩阵

删除 Ghost 投影后，原 runtime 事实仍由原领域持有；重新读取相同来源，不应重学
用户明确删除的投影。相同来源仅修改文字也不能绕过删除；新的 run 身份仍应可用。

初始矩阵：两个消费者（continuity task source、真实 WorkCheckpointStore）× 两个
操作（session delete、reset）× 原文/同身份改文，共八项；另以 continuity 覆盖
user/project × delete/reset，四项，总计 12 项，各一次。每项独立临时 state。
delete 后先重启并 rebuild，确认空投影，再重新消费来源。checkpoint 原始事实必须保留。

停止条件：任何旧来源投影复活即保留阴性结果；不通过删除 runtime checkpoint、放宽 scope
或 scorer 过门。这验证持久来源身份，不替代未落盘在途输入或完整并发生命周期。

## 基线结果

```powershell
python -m pytest tests/test_ghost_deleted_source_lifetime.py -q --tb=short
```

初始 12 项全部失败：delete/reset 成功，重启后的 rebuild 为空，但重新读取旧 task source
或 checkpoint 后又出现相同 run 的投影。八项还覆盖仅改变旧来源文字的情况。
完整基线输出留在本地 `.e2e-artifacts/ghost-control-failures-20261117/source-lifetime-baseline.log`。
回归脚本随随后生产修复提交，基线源码可从上述 Git commit 读取，无需复制 worktree。

这不是 projection generation 的失败：重新消费发生在删除后，取得新 generation，
因此旧的在途快照 fence 按设计不会拒绝。缺口是已删除来源的持久退役语义。

## 下一步

在现有 Ghost SQLite 中持久保存已删除来源身份，按 collection/source/source_ref/scope/scope_ref
隔离；明确删除/reset 时同事务退役，普通过期和容量筛选不退役。正常同步、rebuild 和存储写口
都应尊重该边界。保留 runtime 事实、新 run、其他 scope 和显式控制，生产默认生效。
