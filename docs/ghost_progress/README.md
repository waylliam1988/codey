# Ghost / World Model 进度板

这是长期维护的状态索引。连续大型实验按实验名称或候选功能维护记录；路线图中的版本号只作为可选的验收门关联，不要求每次实验都创建一个版本号。

状态含义、KoboldCpp 故障流程、commit 约定和交接模板见 [团队交接说明](../ghost_handoff.zh-CN.md)。当前流程允许同事直接提交主分支，不要求 PR；生产代码仍必须满足交接说明中的生产合并门槛。

## 当前状态

| 路线图轨道/实验主题 | 状态 | 说明 | 记录 |
|---|---|---|---|
| 历史 memory-routing 归档 | `EXPERIMENT_COMPLETE` | 脚本、脱敏测量、报告和来源摘要已提交；这是实验基线，不是生产晋级 | [实验目录](../../tests/manual/ghost_memory_routing/README.md) |
| 0.6.0 评估冻结与所有权审计 | `EXPERIMENT_COMPLETE` | modelA 单模型 pilot 的矩阵、完整性规则、rank 映射和 admission 所有权已冻结；不改变默认 prompt | [进度记录](frozen-evaluation-ownership-audit.md) |
| 0.6.1 Context Epoch admission | `INCOMPLETE` | shared kernel 已有 fail-closed capture path；尚缺 live smoke、故障注入报告和完整生产矩阵 | [进度记录](context-epoch-admission-wiring.md) |
| 0.6.2 scope 与四个开关 | `INCOMPLETE` | scoped projection、重启/并发 settings、expiry、delete/export 和 kernel provider capture 已验证；尚缺 reset manifest、runtime 并发矩阵与完整 acceptance battery | [进度记录](independent-controls.md) |
| 0.6.3 冲突生命周期与持久删除 | `NOT STARTED` | supersede、tombstone、删除/rebuild 零复活 | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.6.4 确定性 replay 与显式时间 | `NOT STARTED` | 不依赖 wall clock，projection digest 可复现 | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.6.5 类型化 language 与最小遥测 | `NOT STARTED` | language 先 shadow，Hebbian 只作辅助特征 | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.6.6 生产 shadow parity 与排位实验 | `NOT STARTED` | 用真实 builder/operation/admission 复测，而不是只测 harness | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.6.7 language 有条件启用与回滚 | `NOT STARTED` | 只对完整评估通过的模型/任务/配置启用 | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.6.8 command/verification intent shadow | `NOT STARTED` | 先找正确宿主，不默认投 directive | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.6.9 provider outcome canonical event | `NOT STARTED` | 只接真实 send/repair/settlement 的 bounded outcome | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.6.10 correction/repair projection shadow | `NOT STARTED` | 未验证 outcome 只能是 hypothesis/unjudged | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.6.11 Ghost Explain | `NOT STARTED` | 解释选择过程，不重新计算、不产生 evidence | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.6.12 World Model review/shadow context | `SHADOW` | 仅有 bounded prediction/review 设计建议；无真实消费者，不改默认 prompt | [研究建议](world-model-shadow-proposal.md) |
| 0.7 跨模型与留出任务 | `NOT STARTED` | 完整矩阵后按最弱模型收敛 | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.8 Protocol Adaptation | `NOT STARTED` | 独立下游轨道，不扩大 Ghost 权限 | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.9+ 可塑性研究 | `NOT STARTED` | 独立 research benchmark，可随时终止 | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |

## 更新规则

- 只更新事实：commit、测试命令、报告路径、模型/config、样本量、结果和限制。
- 同事可以直接提交主分支；实验 commit、报告 commit 和生产 commit 必须在提交说明中区分。
- `EXPERIMENT_COMPLETE` 必须有实验记录、报告、复现命令和实验 commit；它不自动产生生产合并。
- 只有满足交接说明中的生产合并门槛，才能使用 `PRODUCTION_MERGED`。
- 缺模型、缺对照、缺样本或 KoboldCpp 故障时使用 `INCOMPLETE` / `BLOCKED`，不能用 `EXPERIMENT_COMPLETE`。
- `SHADOW` 是有意不改变默认行为，不等于失败，也不等于生产启用。
- 阴性结果保留；无效实现使用 `EXPERIMENT_ONLY`，如果决定停止一个方向，使用 `ABANDONED` 并写明证据。
- 原始回复、KoboldCpp 输出、state 和完整日志只留本地归档；仓库保存脚本、报告、脱敏指标、fixture 和负结果。

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
