# Sleep 维护控制撤回

- 日期：2026-11-18；关联验收：0.6.2、0.6.4。
- 状态：`PRODUCTION_MERGED`（触发 scope 撤回及单项控制兼容）；基线生产提交：`b9b189c6`。
- 生产提交：Sleep `06c8cfc4`；独立控制 setter `5407430f`，均已进入本地 master 默认路径。
- 原始负结果：撤回 `b260673a`；控制 fallback `00b1663f`，保留原测量与失败归属。
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

### 四开关独立性附加冻结

补充生产边界首轮 **38 passed / 1 failed**，失败属于已有 setter 契约：新状态首次
user `accept_events=false` 同时改写 legacy learning_enabled，使其他未显式存储的控制
跟随 fallback 关闭；Sleep 正确读取了这个错误结果。不能让 Sleep 忽略实际控制来迁就。

在修改 setter 前冻结纯真实控制矩阵：missing、legacy-on、legacy-off、部分显式当前
字段四种设置×四个目标开关×开/关×三 scope，**96 cell**。前后读取全部四开关，只有
目标控制可以改变；project/session 的 allow 不能越过 user deny；冷开结果一致。
保留旧数据的真实有效设置，不猜测历史上哪些关是无意造成。无模型、无采样变量。
任一非目标控制改变即失败；先记录基线，再在同一写事务中 materialize 原四个值后
更新单项。该修复是本轮独立性生产验收的必要依赖，不能删断言或改成全开夹具。

冻结 `5ec65d2c` 后原 inbox 96 项 **92 passed / 4 failed**：user accept_events
从 legacy-off 开启，以及 missing/legacy-on/partial-current 关闭，均改变非目标控制。
[脱敏原始指标](../../tests/manual/ghost_memory_routing/reports/control-materialization-baseline-20261118.json)
保留每项前后值与冷开值。该批尚未修改 inbox；Sleep 候选在同一工作区，未混入 setter 测量。
中间全树检查发现 Sleep compaction closure 的可空默认参数 mypy 错误，已用明确的非空
局部协议变量修正，未放宽类型。其余四个全树门通过；以修复后最终全树复验为准。

## 最终实现与运行验收

Sleep 默认 owner 逐步骤重新读取真实 scoped maintenance 设置，并把可写步骤的读取与
写入放在现有同路径 SQLite 写事务。复合 compaction 逐 store 准入；queue 的过期 claim
回收与 compaction 同事务，后者抛错时前者也回滚。compaction 还逐 store 检查原任务取消
回调。只读 health 不持写锁；关闭、authority 错库和读取失败终止后续步骤并保留 pending
与有界取消/错误报告。独立调用未传 inbox 时也读本 state_home 的真实设置。

单项控制 setter 在改动 legacy learning_enabled 别名前，把四个当前有效基值明确存储。
保留 scoped deny 继承、用户已关闭状态与旧单开关全开/全关语义，不推测历史意图或恢复
曾受旧 bug 影响的配置。inbox 三个排序回调补全参数/返回类型，排序键与顺序不变。

调度补强后原 18 项 **18 passed**；96 项纯控制矩阵 **96 passed**；新增边界最终
**44 passed**。三文件合批 **158 passed**，不是把重复批次数相加。新增覆盖：

- 三 scope，初始禁用、独立缺 inbox 调用、两次冷开拒绝、重新开启后的正常 App 真实维护。
- 其余三控制独立关闭时继续维护既有 accepted state，不新增外部来源授权。
- 设置损坏、控制读错误、错库 authority 均 fail closed，有界报告持久化，修复后可恢复。
- 禁用时显式 scope delete/reset，settings 保留/清除语义及重开；四领域真实事件重建后
  不能恢复旧 allow。没有删除原始历史来获得通过。
- queue compaction 写失败后真实 lease transition/事件/投影完整回滚、可重试；实际控制
  writer 在已准入 queue 写期间竞争，必须等事务完成再提交，提交后无领域变化。
- report 写失败保留上一报告及事件、冷重试；控制关闭时仍保持取消语义。

首批扩大 29 文件 **733 passed / 2 subtests passed**。复查本轮 compaction refactor
发现失败目标未计入 stores_checked，已恢复原来的尝试计数，并给真实回滚测试补精确断言。
随后同一 29 文件最终复验仍 **733 passed / 2 subtests passed**，118.52 秒；无失败或 skip。
最终批前冻结 702 个生产 Python 文件及五个 owned 文件哈希，最终检查和提交后复核一致。
测试范围含正常 post-turn/App、全部实际 Ghost stores、四控制、scope、删除 Manifest
及多进程恢复、projection/delete fence、历史保留、逻辑维护时间、取消与故障隔离；未跑全量 pytest。

复现主矩阵与边界：

```powershell
python -m pytest tests/test_ghost_maintenance_revocation.py tests/test_ghost_control_materialization.py tests/test_ghost_maintenance_control_boundary.py -q -s --tb=short
```

最终扩大批的 XML/日志见本地 `final-expanded.*`；完整受检文件与命令、生产身份、结果、
源文件哈希及明细见[脱敏生产报告](../../tests/manual/ghost_memory_routing/reports/maintenance-control-production-20261118.json)。
详细 digest 来自计数修正前的 158 项批次；最终 733 项在计数修正后的固定源码再次执行
同一 scorer 并通过，不把前一批源码称为逐字节相同。
barrier/竞争等待导致矩阵耗时变化，不能用本批 wall time 宣称生产延迟收益或开销。

最终相关回归完成后，串行全仓 Ruff、完整 `codey` mypy/Pyrefly/ty 和 precision gate 均通过。
五个 owned 文件另行通过三类型检查，额外 mypy `--disallow-any-expr` owned 零诊断；
未修改依赖 334 文件 10864 项诊断使额外审计 exit 1，未修改检查范围或加入豁免。
生产 Pyrefly 158 warnings/35 suppressed 与基线相同。展开 owned 警告后，三项均为
inbox 未改动的 `str()` 兼容转换（control reader/setter 与 scope 编码入口），单列审查，
不称 owned 零警告；没有新增 Any/cast/ignore。未改前端。

## 默认消费者、回滚与剩余审查

普通 AppContext 的 post-turn kick → 默认 GhostSleepDaemon → Sleep owner 直接调用本次逻辑，
控制 CLI/UI 的原单项 setter 直接消费原值 materialization。无需启用新 flag，不新增模型
可见 source 或模型调用；这是模型无关的控制安全修复，不宣称新 family/模型行为收益。
用户关闭仍生效；已获准且持锁的事务可在 deny 提交前完成，不撤销其已提交事实。报告仅是
审计记录，不授予权限、证据或完成状态。故障通过原报告/daemon 路径隔离。

可分别 revert Sleep `06c8cfc4` 与 setter `5407430f` 后正常重启；保留 SQLite 和日志，不 reset 数据
或自动全开控制。setter 已写入的显式值保持其现有意义。回滚 Sleep 会恢复本次已复现的
在途撤回缺陷，需要用既有 run_maintenance 关闭并在任务空闲后重启隔离。

下一步优先独立验证：另一 scope 触发的全局 decay/refresh/compaction，是否修改已 deny
scope 的记录；真实 daemon pending payload 交接与硬退出；不同 store_home 的维护输入。
本轮没有证明这些边界，不把触发 scope 的 18 项外推为整个 scoped maintenance 通过。
其余混版本/retention/purge、完整 G0、语言遥测/family/provider outcome/repair/Explain 仍待验。
World Model 两个重复当前事实候选保持负结果/终止；下一候选必须增加预测信息，再做真实
消费者与单模型收益闭环，不能借本轮静态门或控制安全修复标记生产启用。
