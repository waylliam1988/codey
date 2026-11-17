# Ghost 派生投影与删除交错

- 日期：2026-11-17
- 基线：`17eb2e3e`
- 状态：`EXPERIMENT_COMPLETE`（限定矩阵与候选验收完成）
- 模型 / endpoint：无；不恢复已停止的语言实机批次。
- 实验提交：`eea2a4d9`
- 生产结论：`PRODUCTION_MERGED`（本地 master，限定四个派生 writer 与 continuity replay）
- 生产提交：见 `git log --grep="reject projection writes invalidated by deletion"`。

## 冻结矩阵

2 个操作（project delete / reset）× 4 个真实 writer（continuity refresh、work queue
sync、affinity sync、sleep report）× 2 个顺序（writer 先提交 / 来源读取后 delete 先完成）
× 2 次独立重复，共 32 cell。

各 store 使用真实来源和生产 mutation；在目标 store 的第一次写事务进入前用 Event 暂停，
不替换来源数据或生产 projection 算法。删除走现有默认 control surface，再恢复 writer。
重开 store 并对可 rebuild 的领域 replay，检查目标内容是否复活。

通过条件：旧输入读取和删除交错时，writer 不得重新提交旧投影；writer 先完成时正常删除。
新读取/新输入仍可写入，其他 state home 不应互相干扰。发现复活立即保留阴性结果并修复，
不能通过改变 scorer、补跑语言样本或把 fixture 成功当作完整 mixed concurrency 来过门。

这是存储生命周期收益实验，没有模型收益或 World Model 启用结论。

## 已获得的证据

最终修正后的基线从 Git 对象加载真实 `17eb2e3e` 的 Ghost 模块，在当前工作区运行同一探针，
不 checkout、不复制或新建 worktree：32 cell 中有 18 次删除后复活，全部删除接口仍返回成功。
候选树相同矩阵为 32 cell、0 次复活，
全部删除接口返回成功。报告分别为：

- `tests/manual/ghost_memory_routing/reports/projection-delete-scoped-baseline-20261117.json`
- `tests/manual/ghost_memory_routing/reports/projection-delete-scoped-fenced-20261117.json`

| Writer / 顺序 | 基线复活 / cell | 候选复活 / cell |
|---|---:|---:|
| continuity / delete first | 4 / 4 | 0 / 4 |
| continuity / writer first | 2 / 4 | 0 / 4 |
| work queue / delete first | 4 / 4 | 0 / 4 |
| sleep / delete first | 4 / 4 | 0 / 4 |
| affinity / delete first | 4 / 4 | 0 / 4 |
| affinity、work queue、sleep / writer first | 0 / 12 | 0 / 12 |

两份报告的全部 rebuild 均成功。generation 以同一 state home 为单位；任意 scope 删除都会保守拒绝该库
尚未提交的旧投影，其他 scope 已提交数据保持不变，新一次来源读取可以重试。

初始 fixture 把会话级开放问题用于项目删除，却将保留它们计为复活；这是 fixture 的
scope 假设错误。现已将该输入改为项目 scope，保留原报告
`projection-delete-initial-fixture-20261117.json`，不将其计入验收。判定器没有放宽。

首轮新增回归为 21 passed / 1 failed。补入新确认偏好后的扩大回归为
356 passed / 1 failed、135 subtests passed；仍是 affinity 新来源断言失败。
保留的 SQLite state 证明 affinity 已写入项目节点，真正原因是探针调用
`affinity.list_nodes()` 没传项目 scope，按隔离契约读不到它。修正 helper 读取并在每个
cell 起始断言四个 writer 的种子真实可见后，23 项新增回归通过。生产过滤规则没有放宽。

旧 `projection-delete-baseline-20261117.json` / `projection-delete-fenced-20261117.json`
保留为过程证据：它们漏计 affinity，旧 14 次基线不再作为完整矩阵结论。
最终采用上面的 scoped 报告，基线 18 → 0。原始 fixture 错误与读取错误分别记录，
不能把它们算作生产缺陷，也不能把失效的统计继续当作验收依据。

候选包含两个生产修复：在来源读取到派生投影提交之间，以同库删除 generation 拒绝
过期写入；continuity replay 正确应用 scope deletion event，防止普通 delete→rebuild
也复活旧投影。嵌套 sleep 操作沿用最早 generation，新的来源读取仍允许继续工作。

以上是限定矩阵证据，不代表完整 manifest 生产调度、多进程混合生命周期或 World Model
已完成。下述修复已通过验收并单独进入生产提交；这不扩大上述完整生命周期的完成范围。

## 验收与类型

- 18 个受影响生产回归文件：`357 passed, 135 subtests passed`，包括本轮 23 项新回归。
  覆盖真实 store/control surface、旧 writer、删除、reset、rebuild、重启、独立 state home、
  generation 缺失与冷启动拒绝、新来源继续写入及长项目身份。没有运行全量 pytest。
- 全仓 Ruff 通过；完整 `codey` mypy（699 个模块）、Pyrefly 和 ty 的 Python 3.11 目标均通过；
  `tools.kernel_type_precision_gate` 通过。Pyrefly 为 0 errors、35 个既有 suppression、158 个 warning，
  没有变更配置或新增豁免。
- 两个修改测试和两个实验脚本另经 mypy / Pyrefly / ty 检查，均为 0 errors。
  本轮源文件的动态 JSON/SQLite 输入在边界校验并收窄；不使用 Any、typing.cast 或 type: ignore。
- 完整静态检查命令与输出在本地 `.e2e-artifacts/ghost-projection-fence-20261117/checks/`；
  未完成或被中断的旧检查不计作通过。

## 生产接入与回滚

候选沿用正常 store 写入入口，不新增实验开关或第二条生产路径。schema 4 的同库 generation
在删除事务中推进；四个派生 writer 在读来源前捕获 generation，并在取得写事务后复查。
嵌套维护保留最早值。缺失或不可读时拒绝写入，不能静默恢复为 generation 0。
continuity replay 同时处理删除事件，保留完整项目身份，并允许后续有效事件产生新投影。

用户已有的关闭、隔离及显式删除语义保留。需要暂停维护时可使用既有 run_maintenance 控制。
回滚程序必须保留 generation 和 retirement 数据；schema 3 程序会拒绝 schema 4，
不能通过丢弃删除边界或清空数据库来降级。本轮增加的常量行与检查有额外数据库读取成本，
未测量延迟收益，不宣称完整跨进程恢复或模型行为收益。

## 复现

```powershell
python -m tests.manual.ghost_memory_routing.projection_delete_probe --output <report.json>
python -m tests.manual.ghost_memory_routing.git_projection_delete_probe --revision 17eb2e3e --output <baseline-report.json>
python -m pytest tests/test_ghost_projection_delete_fence.py -q
```

`git_projection_delete_probe` 只从指定提交读取 Ghost Python 模块到内存，其他源码来自当前树。
它复用同一个探针和 scorer；本轮其余生产目录没有修改。按用户要求，不创建或复制 worktree。
只因确证旧 scorer 有遗漏才重测基线，不因续接工作重复有效的历史实验。
不恢复已停止的语言实机批次。实验报告不得替代相关生产回归、全树 Ruff 和三个类型检查。
