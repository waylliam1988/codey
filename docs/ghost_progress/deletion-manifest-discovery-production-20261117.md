# Ghost 删除操作发现与显式恢复入口

- 日期：2026-11-17；比较基线：`ea4223be`（生产基线 `e09a44ac`）。
- 状态：`PRODUCTION_MERGED`（普通 summary/export/action 默认接线，随本记录提交）。
- 依据：[三个响应丢失窗口的冻结负结果](deletion-manifest-discovery-baseline-20261117.md)。
- 这是确定性基础设施修复，无模型调用或新增模型收益主张。

## 冻结合同与实际接线

普通 GhostControlSurface 的 summary/export 默认包含 `deletions`；现有认证
`POST /api/ghost/action` 新增只读 `list_deletions`，不增加 feature flag。
现有 `ghost export --state-home <home>` 会直接打印默认第一页。
API 的直接构造 surface、CLI 的 from_state_home 共用实现，不新增另一份操作记录。

每页最多100个 canonical 操作，默认20；按 operation_id 排序并返回 `next_cursor`。
`after` 是上一页游标；到尾返回空字符串。查询同时覆盖 manifest 行和 event-only 身份，
逐操作 replay 后核对投影、行身份及索引状态。损坏时 action 返回500，export 明确失败，
summary 标记删除读取失败及 health warning，不把错误伪装为没有待处理操作。
输出保留 action、有效 scope、原 ID、时间和各 store 的 pending/blocked 名称，
不输出 store result/detail 或异常正文。

summary 只显示 user/reset 及当前 project/session；list_deletions 可用 scope +
project/session 精确筛选。不带筛选的认证 action 和 export 为操作员设备级视图。
筛选在有界 canonical 页读取后执行，所以空 items 仍可能有 next_cursor，必须继续翻页。
分页不是快照；并发新增的 ID 可能排在已有 cursor 前，刷新时应从头扫描。
不宣称第一页等于所有未完成操作，也不把第一页输出称为完整备份。

恢复仍使用已有 action、原 ID 与原 scope，并显式 confirm；completed 不重复执行，
blocked 保留终态。不会因读取列表而自动重试或开启用户关闭的控制。
没有增加启动自动删除、独立恢复日志或第二套删除 owner。

```json
{"action":"list_deletions","limit":20,"after":""}
```

如列表中的原操作是 project 删除，用其字段恢复：

```json
{"action":"delete_scope","scope":"project","project":"<原project>","delete_operation_id":"<原ID>","confirm":true}
```

session 操作用原 session_id，reset 用原 action=reset_all。blocked 需要显式新操作，
新操作会作用于届时 scope 内的数据，不能把它当作无副作用的同 ID 重试。

## 验收与失败归属

保留原三个硬退出窗口，并扩展 user/project/session/reset，共12个真实进程案例。
全部缺省生成 ID、不返回响应，父进程从普通 export/summary 发现再用原 ID 恢复。
完成后响应丢失的案例另写 fresh 输入，确认同 ID 重试不删新输入。
子进程前后校验全部生产 Python 源码哈希，运行期间不改动生产代码。
另覆盖107操作跨页、空页带游标、scope、非法分页参数、五种损坏、blocked、
关闭设置、真实 CLI export、认证 HTTP action/export/summary 和显式原 scope 恢复。

首次三窗口修复及相关回归 **50 passed / 1 failed**；失败是旧 export 测试把顶层
精确限制为七 store，未包含新增的 deletions。更新预期元数据，保留七 store 导出和
reset 断言；整个测试文件清理既有未注解 helper、动态 fake 和未收窄嵌套 payload。
两个故障 fixture 改为实际 store 上的定点 patch，保留原 health 警告行为验证。

扩展验收首次 **40 passed / 1 failed**；identity 损坏 fixture 在外键开启时直接改主键，
被 SQLite 正确拒绝。仅在该损坏构造连接上关闭外键后再篡改，不改变生产外键配置。
随后 **49 passed / 1 failed**；reset 的 between-store hook 错挂 delete_scope，
未真正触发硬退出。改挂 reset_all 并重新验收，不降低退出码73或恢复断言。

## 最终验证

修正 fixture 后首组110项通过；扩大相关批次 **329 passed**，零失败及 skip，包含
本轮36项发现验收和12项硬退出。329已经包含首组，不相加。命令：

```powershell
python -m pytest tests/test_ghost_manifest_discovery.py tests/test_ghost_manifest_atomic_mutation.py tests/test_ghost_manifest_production_wiring.py tests/test_ghost_deletion_manifest.py tests/test_ghost_deletion_manifest_recovery.py tests/test_ghost_deletion_manifest_multiprocess.py tests/test_ghost_control_surface.py tests/test_ghost_control_mutation_failures.py tests/test_ghost_control_contract.py tests/test_ghost_controls.py tests/test_ghost_cli_split.py tests/test_ghost_language_production.py tests/test_ghost_graph_exact_replacement.py tests/test_ghost_sqlite_store.py tests/durable/test_operation_driver_restart.py tests/durable/test_legacy_chat_rescue.py -q --tb=short --junitxml=.e2e-artifacts/ghost-manifest-discovery-20261117/regression.xml
```

[脱敏逐案例报告](../../tests/manual/ghost_memory_routing/reports/manifest-discovery-production-20261117.json)
保存每项状态及生产树哈希；原 JUnit 留本地。

随后依次通过全树 Ruff、完整 codey 的 mypy（701模块）、Pyrefly（0 errors / 35既有
suppressed / 159 warnings）、ty 和 precision gate。两个修改测试文件另经三种类型
检查通过；其间显式断言三个旧 WorkCheckpoint fixture 的 start 结果非 None，
这些断言已纳入329项的最终执行，未用 cast、ignore、豁免或放宽配置。

首次额外 `--disallow-any-expr` 审计发现两个测试文件共四处表达式推断问题：
空列表、unittest上下文中的集合构造和JSON编码前的内联列表。改用明确元素类型的
中间变量，没有更改断言语义。最终源码对这两个测试文件重跑 **50 passed**：

```powershell
python -m pytest tests/test_ghost_manifest_discovery.py tests/test_ghost_control_surface.py -q --tb=short --junitxml=.e2e-artifacts/ghost-manifest-discovery-20261117/type-cleanup-regression.xml
```

这50项是329项的子集，不相加；报告单列最终复验、四个修改文件源码哈希，并确认生产
源码仍与329项批次相同。随后完整静态门再次依序通过；最终 owned Any 审计另记下行。

最终对两个生产文件及两个测试文件执行：

```powershell
python -m mypy codey/ghost/control_surface.py codey/ghost/deletion_manifest_store.py tests/test_ghost_manifest_discovery.py tests/test_ghost_control_surface.py --python-version 3.11 --disallow-any-expr
```

四个 owned 文件 **零诊断**；363个未修改依赖文件仍有12397项诊断、整体exit1，
不宣称全仓零Any。日志保留本地 `owned-any-final.txt`，同目录另保留首次审计。
没有新增cast、ignore或豁免。`git diff --check` 和 `python -m tools.publish_frontend --check`
通过；没有修改前端、运行全量pytest或恢复停止的模型批次。

## 限制与下一步

CLI 已可发现，携带同 ID 恢复仍走现有 API；没有新增 CLI resume 参数或 UI 恢复按钮。
保留原事件和 schema，回滚可回退本生产接线提交；不会删除操作历史。
完整 retention、升级前 receiptless 输入、异常身份/scope、确定性显式时间 replay
及真实 World Model 闭环仍需推进；本次不宣称完整0.6.3通过。
