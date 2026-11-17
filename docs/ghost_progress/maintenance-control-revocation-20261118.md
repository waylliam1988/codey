# Sleep 维护控制撤回

- 日期：2026-11-18；关联验收：0.6.2、0.6.4。
- 状态：`EXPERIMENT_COMPLETE`（有效生产失败，待修复）；基线生产提交：`b9b189c6`。
- 无模型/endpoint/采样参数：本实验验证真实 App 默认 Sleep 与 SQLite 控制边界，不作模型收益主张。

## 冻结矩阵与判定

目标：确认已排队或正在执行的普通 Sleep 是否在 scoped `run_maintenance=false`
提交后继续写入领域投影/维护事件。固定三个 scope（user/project/session）、三个窗口
（worker 调用前、真实 health 完成后、真实 inbox compaction 完成后）、开启/撤回对照，
共 18 cell，各一次，独立本地 state。真实 AppContext、默认 daemon 和实际 store；
仅用 barrier 调度，不替换维护结果、不伪造控制持久化或 stale-claim 结果。

种入有效 Hebbian 节点和两个真实过期 running queue lease（均早于本轮墙上时间）。
开启组必须完成两个 lease 回收，且有实际维护状态/事件变化；不接受空夹具通过。
关闭由独立线程的真实控制事务提交，以该事务内最后的领域状态为线性化快照。
比较该快照与 worker 结束后的 Hebbian/Affinity/continuity/work_queue 行及事件。
撤回组必须零新增领域变化、原租约不能在提交后被回收，并保存 cancelled 报告。
控制面自身事件和 bounded Sleep 取消报告不算维护泄漏；不允许借报告改写其他领域。

若控制 writer 被已获准事务阻塞，释放 barrier 让该事务结束，允许其在 deny 提交前
完成；提交前的变化计入快照，不算失败。不得同步等待持锁 worker 与 writer 互相完成。
不要求取消已提交事实或中断正在运行的 SQLite 事务。控制提交后的下一次维护准入必须拒绝。
开启对照同样写入 true 并记录快照；保持其余三个开关开启。

主要变量仅为 scope、撤回时机、control enabled；不改维护阈值、权重、TTL、原 scorer。
读取/持久化失败、barrier 超时、daemon 错误、开启组无真实变化均记 fixture/error，
保留日志，不归为生产阴性。18 项全部完成后记录原始结果；复现生产失败后先独立提交
基线，再冻结最小修复。修复后原矩阵不改判定，相关边界及全树静态门通过才生产提交。

本矩阵只验证周期触发 scope 的撤回，不证明另一 scope 触发的全局 decay 能按每条记录
过滤 denied scope；该问题独立审查，不能从本实验推断解决。正常 delete/reset、重启、
控制损坏、事务故障、重新开启与其他开关独立性列入后续生产回归。

原始 SQLite、完整日志/XML 留 `.e2e-artifacts/ghost-maintenance-revocation-20261118/`；
仓库只保存 typed 可复现实验和脱敏指标。停止条件：基线矩阵完成或 fixture 错误；
生产矩阵任意有效 cell 不满足原契约则不晋级，不修改 scorer。

## 基线结果

冻结 `1dd89214` 后有效矩阵 **9 passed / 9 failed**，18 项夹具均有效。
开启组全部回收两个真实过期 lease；三个撤回窗口×三个 scope 均在 deny 提交后继续
修改领域行/原事件，报告均未 cancelled。前两个窗口涉及同步、衰减、continuity 和
queue；第三个窗口仍回收 queue lease。真实普通 App/默认 daemon 已复现，不是仅静态猜测。

首轮 **6 passed / 12 failed**：其中六个 session 项因夹具用 raw session identity，
公开 queue filter 需要 `session_key`，尚未形成完整有效 cell；其余六个关闭失败有效。
修正种入的 queue scope_ref 后重跑完整矩阵，原 scorer/断言未放宽；首轮完整日志保留。

```powershell
python -m pytest tests/test_ghost_maintenance_revocation.py -q -s --tb=short --basetemp=.e2e-artifacts/ghost-maintenance-revocation-20261118/baseline-valid-state --junitxml=.e2e-artifacts/ghost-maintenance-revocation-20261118/baseline-valid.xml
python -m pytest tests/test_ghost_sleep.py tests/test_ghost_sleep_source_controls.py tests/test_ghost_sleep_clock_boundary.py tests/test_ghost_maintenance_as_of.py tests/test_ghost_controls.py tests/test_learning_switch_fail_closed.py -q
```

后者六文件 **120 passed**。指定全仓 Ruff、完整 702 文件生产树 mypy/Pyrefly/ty 和
precision gate 均通过；Pyrefly 158 warnings/35 suppressed 与既有结果相同。
新增实验文件三类型检查通过，额外 mypy `--disallow-any-expr` owned 零诊断；未修改依赖
334 文件 10864 项诊断导致该额外审计 exit 1，不能声称整个项目 Any 清零。
命令/退出码、18 项 digest 和实际变化见
[脱敏基线](../../tests/manual/ghost_memory_routing/reports/maintenance-control-baseline-20261118.json)。

## 生产修复冻结（基线 `b260673a` 之后）

复用原 18 项，保留原 scorer。只在 Sleep owner 复查 scoped maintenance control：
每个写步骤的控制读取与领域写入使用现有同路径 `BEGIN IMMEDIATE`/savepoint；
compaction 按真实 store 分别准入，queue lease reconciliation 与该 store compaction
同事务，不持有整个周期的写锁。只读 health 不包在写事务内，避免其显式 BEGIN 嵌套。
不传 inbox 的独立 Sleep 仍读取自己 state_home 的真实设置，不能缺 authority 就放行；
错路径 authority 或控制读取异常 fail closed。disabled/失败/任务取消均终止后续步骤，
保留有界报告及未执行步骤。原 App/默认 daemon 直接消费，无新开关。

补充回归目标：三 scope 初始禁用、冷开仍禁用、重新开启可恢复；三个其他开关独立性；
损坏设置/错 authority/控制读取失败；mutating step 写失败回滚与恢复；删除/reset 后
重建、重新打开的控制语义；真实 writer 在已准入维护事务期间竞争不出现死锁或越界写。
这些是确定性控制安全修复，不需要重跑已完成的模型收益对照。完整其他 scope 触发的
全局维护过滤仍单独列缺口，本修复不扩大其声明。

### 调度补强与候选首轮记录

候选首轮 **16 passed / 2 failed**：18 项均无 post-commit 领域变化，但两个 compaction
撤回项的 writer 被 SQLite 连续事务竞争推迟到整个周期结束；其报告不 cancelled，
没有证明预定的在途取消，不记通过。保留 `production.log/xml`，不改原 scorer。
为排除该调度歧义，实验只给真实 daemon 的 stop_requested 回调加 barrier：已持锁的
store 释放后，在下次准入前等待真实 writer 提交，仍返回原始 stop 状态。不开启取消、
不伪造维护结果、不在事务内互等。重新运行原生产 `b260673a`，仍 **9 passed / 9 failed**，
18 项有效；见本地 `baseline-scheduled.log/xml`。修复判定与原断言全部保留。
