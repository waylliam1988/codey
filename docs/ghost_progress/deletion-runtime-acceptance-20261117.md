# Ghost 删除与运行时写入交错验证

- 日期：2026-11-17
- 状态：`EXPERIMENT_COMPLETE`（16 cell 的限定交错实验；完整 runtime acceptance 未完成）
- 关联：0.6.2 / 0.6.3；Deletion Manifest Candidate
- 基线：`6f0ddf7e`
- 模型 / endpoint：无；这是存储和恢复边界实验，不调用模型。
- 生产合并结论：`PRODUCTION_MERGED`（限定输入 fence，`17eb2e3e`；完整 manifest 调度未完成）

## 冻结假设和矩阵

验证真实七 store 协调器完成 project delete / reset 后，在删除前已取得输入的 writer
是否能重新提交被删除的身份；完成后重开 store 并 rebuild，检查复活是否持续。
使用隔离临时 state，不触及用户数据。

矩阵：2 个操作（project delete、reset）× 2 个 writer（已接受偏好的 Hebbian
reinforcement、已完成回合的 observation settlement）× 2 个顺序（writer 先提交、
writer 读取输入后等待删除完成再提交）× 2 次独立重复，共 16 cell。
线程用 Event 协调，不依赖 sleep 或随机时序。七个 store 都调用生产 mutation 方法；
不把单独 manifest 并发 append 当成 runtime 并发证明。

成功条件：删除后的旧身份不复活；非删除范围和新身份仍能正常使用。
任何一项复活即为阴性结果，不改变评分或扩样掩盖。另单独验证 store 返回 False
时协调器不得宣称 completed。使用临时隔离 state，不含用户数据；仓库仅保存脱敏指标。

## 基线实测与结论

命令：

```powershell
python -m tests.manual.ghost_memory_routing.deletion_runtime_probe --output tests/manual/ghost_memory_routing/reports/deletion-runtime-baseline-20261117.json
```

基线结果：[16 cell 指标](../../tests/manual/ghost_memory_routing/reports/deletion-runtime-baseline-20261117.json)。
16 个 manifest 都报告 completed，但 delete-first 的 8 个 cell 全部复活；writer-first 的
8 个 cell 无复活。Hebbian 和 observations、project delete 和 reset 结论一致。
复活在重开 store、Hebbian rebuild 后仍可读到，不是 renderer 的显示缓存。

原因：原删除只移除当前记录/事件，未持久作废已删除的输入身份；持有旧 candidate 或
run_id 的独立 writer 可在删除结束后重写。manifest 完成仅证明调用返回，不能证明旧输入
不再有效。这个负结果不支持直接启用 manifest 调度。

实验不使用模型，无语言服从、成本、延迟或跨模型结论。只覆盖上述有确定顺序的两类 writer，
不代表七 store 完整 mixed concurrency；不是对其他来源生命周期的通过声明。

## 后续

若发现复活，先定位身份失效和写入事务边界，再补回归和修复；没有完整证据前不把
manifest 接入默认调度。随后推进混合操作、多进程、失败恢复和范围隔离矩阵。
World Model 的新增消费者依赖这些删除/恢复边界，不能靠另设历史绕过它们。

## 限定生产修复

基线实验提交：`40f24302`。生产修复见 `git log --grep="fence deleted Ghost inputs"`。

在既有 `ghost.sqlite3` 内增加持久的 retired input 身份集合（schema 3），并默认接到
当前控制面的 inbox delete/reset 和 observation delete/reset。身份失效与对应删除同事务提交；
SQLite insert gate 拒绝旧 candidate 的重新投影/强化以及旧 run_id 的重复结算。
这些 gate 在真实生产 store 写入入口生效，不依赖实验开关或内存锁。

同一偏好的新手工确认使用新 candidate identity，仍可正常学习；其他 scope 不受影响。
普通 inbox compaction / 容量筛选不会作废 candidate，只有显式删除/reset 会写入 retirement。
retirement 跨重启/reset 保留；已升级 schema 的 retirement 表缺失时拒绝冷启动，不能静默
创建空表恢复旧身份。v1/v2 数据库正常升级，升级前已删除且无身份记录的历史不补猜。

另修复候选协调器：store 明确返回 False 时记 blocked，不能标为 completed；合法的
0 条删除和 None 返回仍有效。delete_scope callback 使用明确参数 Protocol，移除无限参数签名。
本轮修改文件的 cast 已移除；输入字典验证 key 后收窄到 object-valued mapping，所有本轮新增
函数、测试和脚本参数/返回值均明确，无显式 Any 或新增检查豁免。

复测：

```powershell
python -m tests.manual.ghost_memory_routing.deletion_runtime_probe --output tests/manual/ghost_memory_routing/reports/deletion-runtime-fenced-20261117.json
```

[相同 16 cell](../../tests/manual/ghost_memory_routing/reports/deletion-runtime-fenced-20261117.json)
复活 0；与基线对比为 8 → 0。生产/持久化/控制/语言/各领域受影响回归
`284 passed, 139 subtests passed`，没有运行全量 pytest。包含 21 项新 regression；额外保留
schema 3 的未来版本测试修正：用当前 schema + 1，继续检验未来数据库被拒绝。

全仓 Ruff、完整 `codey` 的 mypy / Pyrefly / ty（Python 3.11 目标）及
`tools.kernel_type_precision_gate` 均通过；mypy 检查 697 个生产模块。8 个本轮 Python 文件
另经三个 checker 单独检查通过。Pyrefly 全树为 0 errors、35 个既有 suppression、158 个
warning；本轮目标 0 errors、4 warnings，没有修改配置或新增 suppression。
命令和输出归档于本地 `.e2e-artifacts/ghost-deletion-fence-20261117/checks/`。

上述输入 fence 是 `PRODUCTION_MERGED` 的限定修复；manifest 默认调度仍未晋级。
本节原有的 continuity/work_queue/affinity/sleep 旧来源快照缺口已由后续
[派生投影 fence](projection-delete-interleaving-20261117.md)限定闭合，不重复当作未修复项。
尚缺：未落盘的在途输入、完整七 store
混合并发与恢复、retirement 存储增长/维护度量。不能将 16 cell 推广为完整删除生命周期通过。
retirement 是永久失效边界，不通过删除表回滚；回滚程序时保留数据库，旧 schema 程序会拒绝
新数据库，不允许以丢弃 tombstone 的方式强行降级。
