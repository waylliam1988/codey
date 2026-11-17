# Ghost 控制面删除与重置失败汇总

- 日期：2026-11-17
- 基线：`5e291b4f`
- 实验状态：`EXPERIMENT_COMPLETE`（限定控制面失败矩阵）
- 生产结论：`EXPERIMENT_ONLY`（本次记录只提交基线证据；修复另行提交）
- 模型 / endpoint：无；这是生产控制面正确性实验，不恢复语言实机批次。

## 假设、矩阵和停止条件

普通 `GhostControlSurface.dispatch_action` 的 delete/reset 结果必须反映 store 的实际
失败；不能在某个 store 返回 False 或 SQLite 报错时宣称删除全部成功。

固定矩阵：七个真实生产 store × 两个操作（project delete / reset）× 两种失败
（显式 False / sqlite3.OperationalError），共 28 cell，各运行一次。使用临时 state，
只在目标 store 的实际方法入口注入故障，不替换控制面汇总逻辑。其余 store 继续真实执行。
目标返回 HTTP 500、ok=false、对应 store 失败码和完整逐 store 结果。

保留合法返回值：删除 0 条和 observation reset 的 None 都是成功，必须用 `is False`
判定。缺失 store 沿用现有 partial surface 的 unavailable 表示；reset 保留用户明确关闭。
这不增加跨 store 原子性，也不证明 manifest 恢复或完整删除生命周期已通过。

发现误报或异常逸出即保留阴性结果并修复，不放宽断言；本实验不使用模型，也不改变
family、prompt、权限或晋级范围。

## 基线实测

基线生产代码加上随后提交的 `tests/test_ghost_control_mutation_failures.py` 初始 39 项回归：

```powershell
python -m pytest tests/test_ghost_control_mutation_failures.py -q --tb=short
```

结果为 **29 failed、10 passed**：

- 14 个 False cell 都返回 HTTP 200、ok=true，没有对应 errors，误报成功。
- 14 个 SQLite cell 都让真实 sqlite3.OperationalError 逸出 dispatcher，未结构化汇总。
- 另一个 partial-failure 回归确认其余真实 observations 已被删除，但汇总仍错误报告成功。
- 10 个正常空结果、设置保留、partial surface 和既有异常行为对照通过。

失败归属为生产汇总逻辑：现有实现仅在 except 中追加错误，没有检查返回 False，
且捕获集合缺 sqlite3.Error。不是旧测试接口或 scope scorer 问题。

## 下一步

修复正常控制面，默认生效；继续尝试其他 store 并准确汇总，不引入实验开关。
清理本轮修改控制面文件的 Any / cast 和未收窄动态输入；扩大相关生产回归并执行全仓
Ruff、完整生产树三个类型检查、kernel precision 和新测试文件的单独类型检查。
生产修复、最终结果和索引更新另行提交。
