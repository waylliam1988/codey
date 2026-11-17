# Ghost 删除与运行时写入交错验证

- 日期：2026-11-17
- 状态：`EXPERIMENT_COMPLETE`（16 cell 的限定交错实验；完整 runtime acceptance 未完成）
- 关联：0.6.2 / 0.6.3；Deletion Manifest Candidate
- 基线：`6f0ddf7e`
- 模型 / endpoint：无；这是存储和恢复边界实验，不调用模型。
- 生产合并结论：`EXPERIMENT_ONLY`

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
