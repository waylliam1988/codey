# Ghost 控制面删除与重置失败汇总

- 日期：2026-11-17
- 基线：`5e291b4f`
- 实验状态：`EXPERIMENT_COMPLETE`（限定控制面失败矩阵）
- 实验提交：`5cc5e31c`
- 生产结论：`PRODUCTION_MERGED`（限定普通控制面失败汇总；生产提交见下）
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

## 生产修复与验收

`GhostControlSurface` 默认汇总显式 False 与 sqlite3.Error；错误 store 的结果为 False，
追加对应错误码，继续尝试其余 store，最终返回 HTTP 500、ok=false。dispatcher 的其他
控制动作也将 SQLite 错误转为结构化失败。没有增加实验开关或另一个删除入口。

合法 0 / None、partial surface 的 unavailable、reset 保留明确关闭的行为保持有效。
新增多 store 同时失败、非删除控制 SQLite 错误和非法 key 请求回归，最终新增 **42 项**。
首轮相关回归 76 passed；扩展到 13 个受影响文件为 **231 passed、6 subtests passed**，
包括控制面、删除 fence、manifest 恢复、多进程冷启动、corruption、语言和真实发送边界。
没有运行全量 pytest，没有恢复已停止的语言批次。

完整 `codey` mypy（699 个模块）、Pyrefly、ty、kernel precision 通过；全仓 Ruff 通过。
新测试另经三个 checker 检查通过。Pyrefly 全树为 0 errors、35 个既有 suppression、
158 个 warning；目标测试为 0 errors、1 warning。未修改配置或新增豁免。
类型清理中 Pyrefly 发现一个空字典的隐式 Any，已补明确的 Mapping[str, object]，重验通过。

本轮控制面移除 Any、cast 和不准确的 reset Protocol，以具体 store union 表达实际方法；
泛型读取保留领域行类型，export callback 和动态 mapping 边界明确。没有放宽生产边界来迁就测试。

生产提交：`git log --grep="report Ghost control mutation failures"`。
回滚可回退本轮控制面提交，保留现有 schema、generation 和 retirement 数据；回退会重新暴露
上述失败误报，不应通过清库或删除 fence 降级。

## 限制与下一步

本轮仅闭合普通控制面的结果表达；失败前已完成的其他 store mutation 不会自动回滚。
durable manifest 默认调度、pending recovery、混合 runtime 并发、在途来源及完整 tombstone
生命周期仍需后续验证和接线，不能由结构化 HTTP 错误推断这些已完成。
