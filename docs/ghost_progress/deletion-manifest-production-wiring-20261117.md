# Ghost Manifest 默认控制面接线

- 状态：`PRODUCTION_MERGED`（普通控制面默认调度，随本记录提交）；比较基线 `e8131422`。
- 前置证据：同库 mutation/ack 原子性、同操作进程/崩溃验收与 36-cell 混合生命周期已通过。
- 目标：普通应用 API、CLI 所用 GhostControlSurface 默认以 Manifest 调度 delete/reset，
  不新增默认关闭的候选开关，不把模型行为收益条件套用到确定性基础设施修复。

## 冻结生产合同

- 缺省生成新的 operation id，响应返回 `delete_operation_id` 及有界 manifest；显式携带
  同 id 重试只处理 pending，原 requested_at 保留。相同 id 改 action 或有效 scope 必须拒绝。
- mutation/ack 沿用每 store 同库事务，七 store 不合为大事务，不跨 canonical 数据库。
  一个 store 失败后继续其余 pending；blocked 是持久终态，需要显式新操作恢复。
- 现有 results/errors 形状及合法 0/None 保留；partial surface 的缺失 store 明确 unavailable，
  不把未提供的 store 写成已完成。不允许不同 Ghost 路径伪装成同库原子性。
- reset 保留显式用户关闭；没有关闭用户开关、scope、generation、退役来源、canonical
  receipt 或权限边界。Manifest 损坏失败关闭，不回退旧删除循环或偷偷换新 operation。
- 结果 detail 不能截断后当作有效 JSON；持久结果使用独立有界上限，超限回滚本次 mutation。
  动态 payload 在边界解码及收窄；本轮涉及文件清除 Any，无 cast/ignore/豁免。

## 冻结验收

正常 action/scope、普通 API 的 direct-construction surface、partial surface、无 id 默认开启，
completed 重试保留新输入、pending 重开恢复、blocked 不重试、不同有效 scope/操作复用拒绝、
同操作并发、不同路径拒绝、损坏失败关闭、长结果及超限原子回滚、reset 保留关闭。
复用已提交的七 store 故障/崩溃及混合矩阵，不重跑语言模型批次。新增相关生产回归后，
完整 Ruff、mypy、Pyrefly、ty、precision gate；本轮测试独立三种类型检查及 owned Any 审计。
接线通过后更新本记录、进度板、roadmap 和交接说明并单独 commit。

## 结果与剩余范围

首批既有控制面及新增入口回归 96 passed。扩展真实 CLI/HTTP 后首轮 31 passed / 3 failed：
一个手写 pending fixture 使用相对 project，而请求经过生产规范化，因此在 scope 复用检查
正确返回 400；fixture 改为明确的绝对项目身份。另两个 HTTP fixture 错把 HTTPConnection
当作 context manager，改为 `closing`。生产断言与认证要求保留，没有让生产路径兼容错误
fixture。修正后结果另记，不把此轮失败删除或计为通过。

扩大批次初轮 **721 passed / 20 failed / 141 subtests passed**，20 个失败均在旧语言
fixture 首次 observation 写入，尚未运行控制 mutation。用 Git 对象在内存装载
`e8131422` 的四个本轮 Ghost 模块复现同一测试，仍在相同位置失败；未复制 worktree
或切换生产源码。canonical receipt 要求已由之前生产提交落地，旧 fixture 直接伪造
完成 observation 不再合法。本轮修正该测试文件为真实 InputQueue 接收、canonical
claim/start、确定性 source task 和真实应用 task entry，保留全部原有行为断言；同时
清理该文件既有隐式 Any，使用明确的 provider 参数和独立 typed loopback capture。

语言首次迁移后 **18 passed / 4 failed**：完整入口比旧 kernel helper 更早准备计数/
元数据 payload，原 callback 在尚未含语言字节时就撤回，故实际发送是合法基线。
诊断中四个 outbound 都不含语言行；不能把这种无故障时序当作发送后撤回验收。
把注入前提冻结为 payload 已含待测语言字节，并另断言确实注入一次，原
`provider_failure` 与零 HTTP 断言保留，四项复验通过。真实任务 fixture 的 state root
按既有短路径惯例缩短，避免 Windows 临时目录/产物清理超长；不改变生产路径实现。
`tests/test_cli.py -k ghost` 的五个相关 CLI 回归另批通过。最终完整相关批次另记。

## 最终验证

修正 fixture 后扩大相关批次 **741 passed / 141 subtests passed**，零失败及 skip；其中新增
默认接线 34 项。批次沿用[原子性候选完整命令](deletion-manifest-atomic-mutation-20261117.md#最终验证)，
增加 `test_ghost_graph_exact_replacement.py`、`test_ghost_manifest_production_wiring.py`、
`test_ghost_language_production.py` 和 `test_ghost_cli_split.py`。原始 JUnit 留本地
`.e2e-artifacts/ghost-manifest-mixed-20261117/default-manifest-regression-final.xml`，
仓库保留[脱敏逐案例报告](../../tests/manual/ghost_memory_routing/reports/manifest-production-wiring-20261117.json)。
JUnit tests=882 包含 141 subtests，不能称为 882 个独立测试函数。

首次额外 Any 审计检出六处 owned 诊断，来自 JSON 条件表达式、pytest fixture 的重载
装饰器，以及两个内联 JSON 字典的上下文推断。改为明确的解码分支、fixture() 和有类型
中间变量；没有用 cast、ignore、豁免或检查配置隐藏。收窄后在最终源码复验 **123 passed**：

```powershell
python -m pytest tests/test_ghost_manifest_production_wiring.py tests/test_ghost_language_production.py tests/test_ghost_control_mutation_failures.py tests/test_ghost_control_surface.py tests/test_ghost_deletion_manifest.py -q --tb=short
```

该 123 项是扩大批次的受影响子集，不与 741 相加；报告单列其最终复验。随后依次通过：

```powershell
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
```

mypy 覆盖 701 模块；Pyrefly 0 errors、35 既有 suppressions、159 warnings。两个本轮
测试文件另经三种类型检查通过，四个生产文件及两个测试文件的最终额外
`--disallow-any-expr` 审计均零 owned 诊断。传递依赖仍有 366 个未修改文件 12451 项诊断、
整体 exit 1，不宣称全仓零 Any；日志留本地 `default-manifest-any-audit-final.txt`。
`python -m tools.publish_frontend --check`、`git diff --check` 通过，没有修改前端或生产模块
清单，也没有跑全量 pytest 或恢复停止的真实模型语言批次。

## 实际消费者、恢复与回滚

`GhostControlSurface._delete_scope/_reset_all` 默认调用持久 Manifest coordinator；应用
`ghost_control_surface(ctx)` 的直接构造和 CLI 的 `from_state_home` 共用这两个方法。
没有新增 rollout 开关，普通 API/CLI 已实际消费。每 store mutation/ack 同 commit，
失败 rollback 后 blocked，控制面继续其余 pending；响应保留 results/errors，并增加
`delete_operation_id` 与 `deletion_manifest`。显式重试沿用响应中的原 ID 和有效 scope，
不重新生成 requested_at；blocked 保留，需要新操作显式恢复该 scope。

CLI 默认生成并打印 ID；携带同 ID 的恢复目前由现有 `/api/ghost/action` 请求提供，
没有另加 CLI 恢复参数或启动时自动恢复。进程在响应前退出而客户端未提供 ID 时的
pending 发现、完整 retention 和升级输入仍是后续工作，不能把本次接线写成全部生命周期完成。
回滚可回退本次生产接线提交，无 schema 降级或事件清除；回退会失去本次默认持久调度与
重复操作保护。用户主动关闭、scope、generation、退役输入及 canonical receipt 约束保留。

完整 retention、升级前 receiptless 输入、异常身份/scope、显式时间 replay 及
World Model 不由本次默认调度证明；有缺口则记录并继续，不能宣称路线图全部完成。
