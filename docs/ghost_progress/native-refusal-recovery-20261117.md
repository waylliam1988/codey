# Ghost 原生回执准入拒绝与恢复组合验收

- 状态：`EXPERIMENT_COMPLETE`（限定共享生产 kernel 组合验收）
- 日期：2026-11-17
- 关联：0.6.1 / 0.6.2
- 基线生产 commit：`36a1f6c5ec41527b5ded295976aabe6e76ad4334`
- 实验 commit：`72c46ad327f527bd4bdd288c91c5b712e4694f5b`
- 生产改动：无；先验证已有唯一 Attempt Gate 和 NOT_SENT 续传契约。
- 生产合并结论：`EXPERIMENT_ONLY`（本次新增测试与记录；所验生产逻辑早已默认接线）

## 冻结目标与矩阵

真实共享 task kernel、SQLite、ApiProvider、GhostInboxStore scoped admit_hints 和
loopback HTTP 联合验证：工具已结算后撤回投递，拒绝必须在 HTTP 前发生；回执保留，
只允许已证明 NOT_SENT 的 attempt 被后继 attempt 替代，恢复不能重复执行工具。

36 cell，每项一次：两个 API 协议（Completions / Responses）×三个控制 scope
（user / project / session）×两种原始工具结果（ok / error）×三种路径：

1. enabled：不开启故障，原生 read_file → done，两次 HTTP。
2. revoked_warm：工具执行后实际关闭 admit_hints；首次续传及仍关闭时重试都拒绝，
   HTTP 保持一次；显式重新开启后，同一暖 provider / SQLite 续传。
3. revoked_cold：同样拒绝，但每次恢复关闭并重开 SQLite/provider，从 canonical
   context snapshot 激活窗口，再由正常 driver 恢复原始 window identity。

主要变量是投递控制及恢复方式；不更改请求内容、scorer、收据或生产 gate。
每个 revoked cell 要证明：两次 RequestPrepError/error/NOT_SENT、原始工具仅执行一次、
恢复保留原 call id/结果及 window identity、成功投递后原批次只有两个 superseded attempt。
终止条件：36 项收齐；生产契约失败保留负结果，夹具问题单列且不计通过。

## 模型、证据与限制

无外部模型调用。模型 ID 为 deterministic-native-receipt-fixture，endpoint 是隔离本机
HTTP 服务；测试响应固定，不评估模型推理、语言收益、成本或跨模型能力。
工具结果由类型明确的fixture执行回调返回；工具调度、结算、回执投递和恢复使用真实生产路径。
不重跑已停止的 /api/run 20/20/12 批次。这是实际共享生产 kernel 的组合证据，
不等于普通 /api/run 完整恢复、硬退出、repair/compaction 或完整 0.6.1/0.6.2 验收。

运行前冻结全部 codey/**/*.py 的 SHA256，运行后校验文件数和 hash 不变；运行日志与
完整 state 仅在 .e2e-artifacts，仓库保留测试、脱敏报告及文档。

## 结果、检查与下一步

首轮36项均已到达最终成功与不重复执行断言，但在审计查询处失败：测试用模型工具名
read_file查找持久运行时工具名，实际规范名为read（task_effects._runtime_name）。
这是夹具错误，不是生产丢回执；改为查询既有规范名，保留全部结果、call id和批次断言。
首轮日志/XML保留在本地baseline产物，不把这轮计为36个生产负结果或通过。
随后按冻结矩阵复验；不添加重复gate或默认关闭开关。

复验36/36通过，相关12文件187项全部通过，零失败/错误/跳过。
enabled12项各两次HTTP、工具执行一次；revoked24项在两次拒绝期间各保持一次HTTP，
两次均为RequestPrepError/error/not_sent；显式重开后总HTTP两次、工具仍只执行一次。
冷恢复12项各重开SQLite/provider两次，真实持久控制仍关闭；加载canonical上下文后，
driver恢复原始window identity。最终原call id、成功/错误receipt、原始工具结算和已投递批次
均保留，两个拒绝attempt被既有NOT_SENT机制替代。

```powershell
python -m pytest tests/test_ghost_native_refusal_recovery.py tests/test_ghost_attempt_gate_lifecycle.py tests/test_ghost_provider_send_boundary.py tests/test_provider_not_sent_settlement.py tests/test_tool_result_delivery.py tests/test_api_provider_request_policy_lifecycle.py tests/durable/test_provider_response_receipts.py tests/durable/test_provider_context_authority.py tests/durable/test_operation_control_actions.py tests/durable/test_not_sent_continuations.py tests/test_ghost_controls.py tests/test_ghost_language_production.py -q --tb=short
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
python -m mypy tests/test_ghost_native_refusal_recovery.py --python-version 3.11
pyrefly check tests/test_ghost_native_refusal_recovery.py --python-version 3.11
ty check tests/test_ghost_native_refusal_recovery.py --python-version 3.11
python -m mypy tests/test_ghost_native_refusal_recovery.py --python-version 3.11 --disallow-any-expr
```

全树Ruff、三个生产类型检查及precision gate通过；新增测试三个检查通过，Pyrefly无警告。
首轮额外Any审计发现测试Thread kwargs字典受标准库签名影响被推断为dict[str, Any]；
改为显式None返回的serve闭包，不添加豁免。187项是此类型修订前的扩大回归；
修订后独立复验36项及完整静态门，结果以[脱敏报告](../../tests/manual/ghost_memory_routing/reports/native-refusal-recovery-20261117.json)为准，
两批不相加。全树生产Pyrefly保持0 errors / 35既有suppressed / 158 warnings，
不能据此宣称全仓没有Any。

最终类型修订后的36项全部通过；全树Ruff、mypy/Pyrefly/ty、precision gate再次通过。
新增测试三个类型检查及额外Any审计零owned诊断。额外审计命令exit1仅来自未修改依赖
的5895项/230文件诊断；这些没有算为本文件通过，也没有改检查范围或豁免。
三轮runtime批次均冻结并核对全部702个生产Python文件，生产源码无变化。
最终测试LF源码SHA256：`081dec58a0a961c1e45382d89eb24b9cf99d879b7f832df92cd0e639d7eaf2f7`。

## 生产接线、回滚与剩余范围

ApiProvider已有唯一物理attempt gate把明确准入拒绝表达为RequestPrepError；kernel使用
既有error/NOT_SENT结算及续传，不添加第二gate、补造历史或扩大错误分类。本轮无需生产修复，
也没有待开启开关。用户关闭时保持拒绝；只有显式重新开启才恢复投递。

本轮测试和记录可独立回滚，不影响已启用中文偏好或生产恢复机制。历史直接GhostError实验
原始负结果保留，但不再把它描述为当前生产尚未采用prep契约；本次限定闭合的是同一run的
native成功/错误回执在真实HTTP拒绝后的暖/冷恢复。没有验证硬退出、未知已发送结果、
跨run、完整App入口、repair/compaction和全部并发scope，不宣布0.6.1/0.6.2或World Model完成。

下一步补齐剩余发送/恢复边界，避免重做已有NOT_SENT机制，再继续真实World Model消费者。
