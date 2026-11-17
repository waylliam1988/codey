# Ghost 混版本 observation reader 容量窗口基线

- 日期：2026-11-17；冻结当前生产基线：`8cffa97c`。
- 历史 reader：真实 Git `7244c1e3` schema7，通过 Git 对象导入，不复制 worktree。
- 状态：`EXPERIMENT_COMPLETE`（有效基础设施负结果）；关联 0.6.3；生产结论：`EXPERIMENT_ONLY`。
- 模型/endpoint：不使用；明确移除 embedding 配置，不恢复已停止的语言批次。

- 后续：[schema9 兼容消费视图](observation-consumption-view-production-20261117.md)已在默认
  owner 构造启用，将原 15 cell 全部修复为通过；本文件保留原始 13/2，不代表实际模型
  发送、旧快照/内存或整个混版本 App 已验证。

## 冻结目标、变量和验收

假设：schema8 保留原始 observation 历史并推进消费窗口后，已运行的 schema7
公开检索及历史渲染消费者不会重新消费窗口外的记录。原始 SQL 审计可读不算失败；
本实验分别观察 read_committed、retrieve 和 render_retrieved_block，不能称为实际 provider 发送。

真实旧进程正常创建 schema7 store、通过 append_completed 种入一个可检索词，证明其
在受测 scope 可检索并发出 ready，然后持有原 store 等待。父进程按阶段升级同库，再
释放旧 reader。不修改旧方法、不伪造版本、不引入旧代码 checkout。

user/project/session 三个 scope × 五个阶段，共 15 cell，每项一次：

1. schema7_visible：未升级、低容量，三个旧消费者都应能读到原记录。
2. schema7_trimmed：旧进程加入真实 5000 条不同项目/会话的较新记录后，通过旧
   append_completed 正常裁剪；原记录应不可检索。
3. capacity：当前进程升级、加入同样的 5000 条其他 scope 记录，并通过当前
   append_completed 推进真实窗口，原始记录保留、当前消费者看不到原记录。
4. delete：在 capacity 基础上通过普通控制面 delete_scope 完成持久删除。
5. reset：在 capacity 基础上通过普通控制面 reset_all 完成重置。

后三阶段都写入新身份、原项目/会话的另一个检索词，证明当前和旧消费者仍可读到
合法新记录，而不是整体停用检索。旧版裁剪对照也写入该新词；低容量对照不写。
当前消费者及原始 SQL 保留断言先通过，旧进程正常退出、四个结果均为 bool，且
新词符合预期后，才进入最终原词消费比较。所有准备、通信、子进程与基础设施错误
分开归因，不能计作有效泄漏。主要指标是窗口外旧词进入公开检索/渲染的 cell 数，
目标 0；三个阶段结果分别保存，不把 read_all 的审计含义混入指标。

达到 15 cell 后停止；不因阴性或阳性增加 reps、改变 scorer 或降低真实容量。
生产 Python 在所有运行中冻结。结果决定是否需要独立生产修复；本实验不验证
vector、旧版完整 App、在途 SELECT 快照、模型发送、其他六个领域或物理 purge。

```powershell
python -m pytest tests/test_ghost_legacy_observation_reader.py -q --tb=short --junitxml=.e2e-artifacts/ghost-legacy-reader-20261117/baseline.xml
```

## 原始结果与失败归属

**15 cell 全有效：13 passed / 2 failed**。所有失败都进入最后的三结果比较，
没有准备、scope、新记录可用性、当前消费者、原始历史保留或进程通信错误。

| 阶段 | user | project | session |
|---|---|---|---|
| schema7_visible | 通过，旧词可读 | 通过，旧词可读 | 通过，旧词可读 |
| schema7_trimmed | 通过，旧词不可读 | 通过，旧词不可读 | 通过，旧词不可读 |
| capacity | 通过，默认最近 200 条不含旧词 | **失败，三消费者均读到旧词** | **失败，三消费者均读到旧词** |
| delete | 通过，旧词不可读 | 通过，旧词不可读 | 通过，旧词不可读 |
| reset | 通过，旧词不可读 | 通过，旧词不可读 | 通过，旧词不可读 |

两个失败的 read_committed、retrieve、render_retrieved_block 都返回旧内容，同时
合法新词仍可检索。user 通过仅证明当前默认最近 200 条扫描不包含该记录，不能
外推为任意 limit、任何 user 数据布局都遵守窗口。delete/reset 对照说明已有永久
来源退役可阻止这两个公开旧消费者，但不补齐 capacity 的非删除语义。

归属为生产 schema8 原始保留表与旧 reader 读取契约不隔离：旧实现没有窗口
谓词，按 scope 筛选后仍可找到全局最近 5000 窗口外的旧记录。当前实现已正确
过滤窗口，所以这不是当前 reader 回归，也不是纯 SQL 审计记录暴露。

后续独立评估存储层消费视图/兼容隔离，保留原始表的审计与恢复能力。不能简单
把容量过期等同用户删除，将其写进永久来源退役：该登记同时影响其他领域的
run 来源资格，可能误伤仍合法的 continuity/work queue。也不能只给当前 Python
增加版本检查，声称已经约束本次真实在运行的旧代码。

相关回归 **236 passed**，包括既有窗口、reset、损坏、SQL store、graph、vector、
experience 和物理删除约束；生产 702 个 Python 文件在原实验和相关运行中哈希
保持不变。未运行全量 pytest、模型或已停止语言批次。

```powershell
python -m pytest tests/test_ghost_event_windows.py tests/test_ghost_reset_history_retention.py tests/test_ghost_reset_boundaries.py tests/test_ghost_corruption_contract.py tests/test_ghost_graph_sqlite.py tests/test_ghost_sqlite_store.py tests/test_ghost_vectors.py tests/test_experience_memory.py tests/test_ghost_history_delete_guards.py -q --tb=short --junitxml=.e2e-artifacts/ghost-legacy-reader-20261117/baseline-related.xml
```

完整日志、JUnit 和失败 state 只留本地 `.e2e-artifacts/ghost-legacy-reader-20261117/`
及 pytest 保留目录；仓库只保存脱敏逐 cell 结果。本记录不代表原始删除约束失效，
不宣布完整混版本 runtime、0.6.3 或 World Model 通过；没有本轮新增生产开关。

## 类型审计与复现检查

首轮全树门和两个新增文件的三种类型检查均通过，但额外 Any 审计发现测试直接
检查 SQLite fetchone() 表达式时带入一项 Any。已将结果明确收窄为 sqlite3.Row | None，
未修改生产、旧代码、数据、指标或行为断言。类型修正后完整 15 cell 再跑，仍为
同样的 **13 passed / 2 failed**；两项仍在最终三消费者结果比较失败。
原始与复验日志均保留，两个批次不相加。

复验后串行全树 Ruff、mypy（702 个生产模块）、Pyrefly、ty 与 precision 全部通过；
Pyrefly 为 0 errors、35 项既有 suppressed、158 warnings。两个新增文件另经三种
类型检查通过，额外 Any 审计 owned 零诊断；未修改依赖 187 文件 4065 项诊断使
扩展命令 exit 1，不能称为全树 Any 审计通过。没有 cast、ignore、新豁免或弱化检查。
最终日志为 baseline-final-*，首轮 baseline-* 及两次独立 15-cell JUnit 保留。

[脱敏逐 cell 报告](../../tests/manual/ghost_memory_routing/reports/legacy-observation-reader-baseline-20261117.json)
记录两次独立结果、类型修正、检查范围与 LF 规范化源码摘要。

```powershell
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
python -m mypy tests/test_ghost_legacy_observation_reader.py tests/manual/ghost_memory_routing/legacy_observation_reader.py --python-version 3.11
pyrefly check tests/test_ghost_legacy_observation_reader.py tests/manual/ghost_memory_routing/legacy_observation_reader.py --python-version 3.11
ty check tests/test_ghost_legacy_observation_reader.py tests/manual/ghost_memory_routing/legacy_observation_reader.py --python-version 3.11
python -m mypy tests/test_ghost_legacy_observation_reader.py tests/manual/ghost_memory_routing/legacy_observation_reader.py --python-version 3.11 --disallow-any-expr --no-error-summary
```
