# Ghost / World Model 进度板

这是长期维护的状态索引。连续大型实验按实验名称或候选功能维护记录；路线图中的版本号只作为可选的验收门关联，不要求每次实验都创建一个版本号。

状态含义、KoboldCpp 故障流程、commit 约定和交接模板见 [团队交接说明](../ghost_handoff.zh-CN.md)。当前流程允许同事直接提交主分支，不要求 PR；生产代码仍必须满足交接说明中的生产合并门槛。

2026-11-17 晋级要求修订：一个目标本地模型及配置的行为收益与相关生产边界通过，即可在受测范围
正式启用并按合并事实标记 `PRODUCTION_MERGED`。第二模型与跨模型验证是可选扩展，不阻塞单模型晋级。
本次只修订验收范围，历史测量与已有完成、合并状态不自动变化。

## 当前状态

| 路线图轨道/实验主题 | 状态 | 说明 | 记录 |
|---|---|---|---|
| 派生投影与删除交错 2026-11-17 | `PRODUCTION_MERGED`（限定投影 fence） | 修正 scope 读取后的 32 cell 基线复活 18、生产修复 0；四个派生 writer 与 continuity replay 默认生效；357 项相关回归与完整静态检查通过；Git 对象复现不复制 worktree | [矩阵、scorer 修正与验收](projection-delete-interleaving-20261117.md) |
| 删除与旧 writer 交错 2026-11-17 | `PRODUCTION_MERGED`（限定输入 fence） | 基线 16 cell 中 8 次删除后复活；生产候选/run 身份作废 gate 后复活 0；284 项相关回归通过，完整 manifest 调度和混合生命周期仍待完成 | [冻结矩阵、负结果及修复](deletion-runtime-acceptance-20261117.md) |
| 已验证中文偏好生产启用 2026-11-17 | `PRODUCTION_MERGED`（受测模型与普通 Chat） | 中文 typed field、UI、语言优先与真实发送前复查已接线并默认启用；沿用既有单模型正结果，新增实机批次按用户要求停止；已逐项核对其他实验消费者 | [启用与遗漏核对](language-production-enablement-20261117.md) |
| 历史 memory-routing 归档 | `EXPERIMENT_COMPLETE` | 脚本、脱敏测量、报告和来源摘要已提交；这是实验基线，不是生产晋级 | [实验目录](../../tests/manual/ghost_memory_routing/README.md) |
| 0.6.0 评估冻结与所有权审计 | `EXPERIMENT_COMPLETE` | modelA 单模型 pilot 的矩阵、完整性规则、rank 映射和 admission 所有权已冻结；不改变默认 prompt | [进度记录](frozen-evaluation-ownership-audit.md) |
| 0.6.1 Context Epoch admission | `INCOMPLETE` | 请求 scope、过期和首轮后撤回已在当前 Attempt Gate 上完成 loopback HTTP 拒绝；完整 provider/send 矩阵、repair/resume/compaction 边界和 live/off 对照仍缺口 | [进度记录](context-epoch-admission-wiring.md) |
| Provider Send Scope Authority | `PRODUCTION_MERGED` | `3d013af1` 修复候选自授权、混合 scope 误拒绝与长身份截断别名；有效 on/off 请求 payload 不变 | [子实验](provider-send-scope-authority.md) |
| Provider History Expiry | `EXPERIMENT_COMPLETE`（负结果已由当前 Attempt Gate 闭合） | `bf7f1b8f` 的旧实现负结果保留；`3d025cce` 在当前 commit 上以 text/native × 过期/不过期 4 cell 验证，过期请求在 HTTP 前拒绝 | [负结果与闭合记录](provider-history-expiry.md) |
| Scoped Control Failure Closure | `PRODUCTION_MERGED` | `34e201dc` 移除 unscoped/learning fallback；缺 authority 不学不写，settlement 尊重项目/会话 deny；实际 entry→HTTP on/off/error 与扩展回归已验证 | [修复与验证](scoped-control-failure-closure.md) |
| Provider Control Revocation | `EXPERIMENT_COMPLETE`（负结果已由当前 Attempt Gate 闭合） | `c39f0897` 的旧实现负结果保留；当前 commit 上 text/native × 开启/初始关闭/首轮后关闭 6 cell 均验证，撤回后第二次请求在 HTTP 前拒绝 | [负结果与闭合记录](provider-control-revocation.md) |
| Final Payload Admission Placement | `EXPERIMENT_ONLY` / `INCOMPLETE` | 60 项定位 lease/retry/辅助发送时隙；`3b04b8a6` 的独立 66 项证明显式 JSON 表示可修复编码误拒，但 history-only 摘要仍缺来源继承，暂不合并 | [宿主实验](final-payload-admission-placement.md) |
| Admission Refusal Settlement | `EXPERIMENT_ONLY` / `INCOMPLETE` | `b1a78723`：8 项真实 durable 对照，0 HTTP 的 GhostError 被记为 MAYBE_SENT；已有 prep 契约为 NOT_SENT，尚未证明完整 native 恢复 | [拒绝与恢复边界](admission-refusal-settlement.md) |
| Protocol Source Lifetime | `EXPERIMENT_ONLY` / `INCOMPLETE` | `603f01f` / `36566995`：真实 task/restart 证明 hint 不在 durable context；去掉重复 export 的消融将 reopen 撤回泄漏 2/2→0/2，warm 仍 2/2；checkpoint 语义待验，不加第二套 journal | [归属、消融与复现](protocol-source-lifetime.md) |
| Canonical Checkpoint Unification | `EXPERIMENT_ONLY` / `INCOMPLETE` | `ec89d516` / `0760ed08`：48 cell 证明仅删 export 在窄预算会丢事实；canonical-only 摘要→既有 context_edit 保留事实且无撤回泄漏，生产事务/消费者待接 | [统一实验与生产边界](canonical-checkpoint-unification.md) |
| Canonical Context Source Commit | `EXPERIMENT_ONLY` / `INCOMPLETE` | 隔离候选补齐 head + revision 来源事务与顺序折叠；42 cell 否定仅 revision 的删除重建安全，扩大回归 648 passed / 5 既有 skip；默认 compaction / warm / attempt gate 仍待接 | [候选源码、负结果与复现](canonical-context-source-commit.md) |
| Canonical Context Production Wiring | `INCOMPLETE`（生产代码已在本地 master） | `3d025cce`：durable context_edit、Responses 来源映射、cutoff folding、唯一 physical Attempt Gate、dispatch binding 已进入本地 master；代码验证通过，但完整 0.6.1 acceptance 仍缺 | [生产接线与边界](canonical-context-production-wiring.md) |
| Attempt Gate history / revocation closure | `EXPERIMENT_COMPLETE`（限定边界） | 当前 Attempt Gate 在 4 项 expiry 与 6 项撤回 loopback 对照中均于 HTTP 前闭合；完整 0.6.1/0.6.2 仍未完成 | [实验记录](attempt-gate-history-revocation-closure.md) |
| Attempt Gate lifecycle acceptance | `EXPERIMENT_COMPLETE` | 生产 ApiProvider 的 native follow-up、错误 receipt、恢复上下文、compaction auxiliary 和 terminal receipt 均验证统一 gate 边界；仍是 fixture 离线证据 | [实验记录](attempt-gate-lifecycle-acceptance.md) |
| Language Live Evaluation 2026-10-21 | `EXPERIMENT_COMPLETE`（受测中文子集已生产启用） | modelA/config 的 language on/off 为 20/20 对 0/20；2026-11-17 接入普通 Chat 并默认启用，无需第二模型 | [实验记录](language-live-evaluation-20261021.md) / [生产启用](language-production-enablement-20261117.md) |
| 0.6.2 scope 与四个开关 | `INCOMPLETE`（控制已生产接线） | 四开关已默认接线，显式关闭仍生效；scoped 故障关闭及首轮后撤回已验证；完整 reset manifest、runtime 并发和 acceptance battery 仍缺 | [进度记录](independent-controls.md) |
| Deletion Manifest Candidate | `EXPERIMENT_COMPLETE`（未接入生产 store 调度） | 已验证 GhostSQLite 持久化、七 store 方法绑定、重启、幂等、delete/reset 七 store 失败恢复和 7-process 冷启动；真实 runtime 混合并发与完整 tombstone/rebuild 仍未完成 | [候选记录](deletion-manifest-candidate.md) / [delete 失败恢复](deletion-manifest-failure-recovery.md) / [reset 失败恢复](deletion-manifest-reset-recovery.md) / [并发冷启动](deletion-manifest-multiprocess.md) |
| Research parity state contract | `PRODUCTION_MERGED`（state 契约） | 核对确认 research 生产依赖已显式携带 state；缺失 Ghost control fail closed，无待开的实验开关；不改变默认 prompt | [进度记录](research-state-contract.md) |
| 0.6.3 冲突生命周期与持久删除 | `INCOMPLETE`（输入与派生 writer fence 已生产接线） | 候选/run 身份失效、派生投影 generation 与 continuity 删除 replay 已默认生效；完整 manifest 调度、混合并发及在途来源生命周期仍待完成 | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.6.4 确定性 replay 与显式时间 | `NOT STARTED` | 不依赖 wall clock，projection digest 可复现 | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.6.5 类型化 language 与最小遥测 | `INCOMPLETE`（中文子集已接线） | 中文 typed field、UI 与模型/配置拒绝诊断已生产接线；其余遥测计划不作为本次已验证子集启用前置 | [生产启用](language-production-enablement-20261117.md) |
| 0.6.6 生产 shadow parity 与排位实验 | `INCOMPLETE`（已有生产发送回归） | 真实 entry/HTTP 验证通过；新增完整实机与排位矩阵未完成，按用户要求不再补跑 | [生产启用](language-production-enablement-20261117.md) |
| 0.6.7 language 有条件启用与回滚 | `INCOMPLETE`（受测中文子集已默认启用） | Gemma 12B/受测预算的普通 Chat 已启用并可关闭、删除和回退；不代表完整规划项完成 | [生产启用](language-production-enablement-20261117.md) |
| 0.6.8 command/verification intent shadow | `NOT STARTED` | 先找正确宿主，不默认投 directive | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.6.9 provider outcome canonical event | `NOT STARTED` | 只接真实 send/repair/settlement 的 bounded outcome | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.6.10 correction/repair projection shadow | `NOT STARTED` | 未验证 outcome 只能是 hypothesis/unjudged | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.6.11 Ghost Explain | `NOT STARTED` | 解释选择过程，不重新计算、不产生 evidence | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.6.12 World Model review/shadow context | `SHADOW` | 仅有 bounded prediction/review 设计建议；无真实消费者，不改默认 prompt | [研究建议](world-model-shadow-proposal.md) |
| 0.7 留出任务与可选模型扩展 | `NOT STARTED` | 逐模型独立验收；第二模型为可选扩展，不阻塞已通过单模型的生产启用 | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.8 Protocol Adaptation | `NOT STARTED` | 独立下游轨道，不扩大 Ghost 权限 | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.9+ 可塑性研究 | `NOT STARTED` | 独立 research benchmark，可随时终止 | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |

## 更新规则

- 只更新事实：commit、测试命令、报告路径、模型/config、样本量、结果和限制。
- 同事可以直接提交主分支；实验 commit、报告 commit 和生产 commit 必须在提交说明中区分。
- `EXPERIMENT_COMPLETE` 必须有实验记录、报告、复现命令和实验 commit；它不自动产生生产合并。
- 只有满足交接说明中的生产合并门槛，才能使用 `PRODUCTION_MERGED`。
- 生产合并门槛只要求一个目标模型及配置；未测试的其他模型不算该模型的缺失数据，也不将其限制为试点或 `SHADOW_ONLY`。部署范围必须注明模型、任务和配置。
- 目标模型缺失、缺对照、缺样本或 KoboldCpp 故障时使用 `INCOMPLETE` / `BLOCKED`，不能用 `EXPERIMENT_COMPLETE`。
- `SHADOW` 是有意不改变默认行为，不等于失败，也不等于生产启用。
- 阴性结果保留；无效实现使用 `EXPERIMENT_ONLY`，如果决定停止一个方向，使用 `ABANDONED` 并写明证据。
- 原始回复、KoboldCpp 输出、state 和完整日志只留本地归档；仓库保存脚本、报告、脱敏指标、fixture 和负结果。
- 研究验证优先实验必需测试、受影响生产入口和适度扩展回归；按用户 2026-10-21 最新要求，
  不把每轮全量 pytest 设为研究前置。发现失败仍保留证据、区分 fixture/production，不放宽门槛。

## 实验记录模板

复制下面内容到 `docs/ghost_progress/<experiment-id>.md`：

```markdown
# Ghost <experiment-id> 进度记录

- 实验名称：
- 状态：
- 负责人：
- 开始日期：
- 最后更新：
- 关联 Roadmap 验收门（可选）：
- 实验 commit：
- 生产合并结论：EXPERIMENT_ONLY / SHADOW_ONLY / PRODUCTION_MERGED

## 本次目标

## 实际改动

## 验收命令与结果

## 实验配置

## 结果与限制

## 已知问题和回滚方式

## 下一步
```
