# World Model shadow 研究建议

- 实验名称：World Model bounded prediction review
- 状态：初始设计；首个消费者已有独立原型与实机负结果（见下）
- 负责人：Codex
- 最后更新：2026-11-18
- 关联 Roadmap 验收门：0.6.12 World Model review/shadow context
- 实验记录：[验证预测复盘消费者](world-model-verification-review-20261118.md)
- 生产合并结论：`SHADOW_ONLY`

2026-11-18：[验证结果预测复盘消费者实验](world-model-verification-review-20261118.md)
已完成隔离原型及本地 Gemma 的 40 次真实请求；严格 JSON action on/off 均 0/20，
输入 token 增加而无冻结指标收益，终止该接口候选。本文保留初始设计；World Model
已不再只有提案，但尚无通过收益/生产边界的正式消费者，不应标为生产启用。

2026-11-17 任务范围更新：本轮需要在 Ghost 生命周期与 admission 依赖通过后推进真实
prediction→review→projection→consumer→评估。下文“当前仅有提案”的阻塞为初始设计历史；
下文默认关闭适用于未验证阶段。一个实际本地模型/配置证明净收益且相关生产边界通过后，
在受测范围正式接线并默认启用，不等待第二模型，也不永久停留于 SHADOW_ONLY。

## 研究目标和假设

目标是评估一个有界的 World Model 投影是否能帮助 Codey 发现“下一步可能需要
复盘或验证的状态”，同时保持 Ghost 的权限边界。候选功能只产生预测候选和
review queue，不产生事实、Evidence、CompletionProof、工具调用或最终 prompt
指令。

假设：只有存在真实消费者（例如任务结束后的 prediction review、恢复时的
bounded projection 或用户显式查看）时，预测候选才有研究价值；没有消费者时，
增加 World Model 只会增加存储和上下文噪声。

## 推荐研究轨道

1. **事件输入**：只接受已提交的 bounded run facts、provider outcome、verification
   结果和用户显式修正；每条输入带 scope、run/session ref、时间和 digest。
2. **预测投影**：输出 `hypothesis`、`expected_next_state`、`confidence`、
   `source_refs` 和 `expiry`。`source_refs` 只能指向已有 trace/ledger ref，不能
   填充 Evidence 或 citation。
3. **review 消费者**：在任务完成、恢复或用户显式请求时显示“待复盘预测”，由
   Codey review/verification 决定 `confirmed`、`rejected` 或 `unjudged`。
4. **admission**：只有通过独立 `ContextSource`、scope、expiry、budget 和
   `admit_hints` gate 的 bounded review summary 才能进入模型；默认关闭。
5. **停止条件**：没有真实消费者、没有可复现的 prediction review 指标、或预测
   不能稳定改善恢复/验证成本时，保留 `EXPERIMENT_ONLY` 代码和负结果并停止。

## 初始实验建议（历史；实际冻结见独立记录）

- 模型 / suite / endpoint：先离线 deterministic fixture；需要模型时另行记录
  endpoint、model ID 和上下文长度；不复用 admission smoke 的 20/20 作为 World
  Model 证据。
- tasks / cells / reps：至少覆盖 task completion、provider failure recovery、
  verification correction 三类任务，每类保留 no-model replay 和 review consumer
  对照；最低样本量、scorer 和成本预算在实现前冻结。
- on/off 对照：无预测 projection、shadow projection 但不发送、显式 review
  consumer；不得把 shadow 线直接加入默认 prompt。

## 当前阻塞和边界

- 已完成首个隔离消费者及真实模型评估，受测格式接口没有净收益，因此尚无获准生产的
  World Model consumer，不新增默认 prompt section。
- 原型的纯投影源存活/过期/admission 检查不等于完整生产 scope/restart/delete/reset；
  后续有收益的候选仍须补齐自身生产边界，不能据原型验收宣布完整 0.6.2 通过。
- 原始 prompt/reply、模型输出和完整 state 仍只留本地归档；未来仓库只保存
  deterministic fixture、脱敏指标和负结果。

## 下一步

首个文本复盘→严格 action 接口已按冻结门标记 `ABANDONED`，不进入 runtime。
若继续验证复盘方向，另行冻结更贴近正常消费者的结构化接口及有新增预测信息的任务；
保留原 scorer 与负结果，不重做已完成原型或将补跑的最好结果替代历史。完整生产边界
仍按消费者逐项验收，单模型有收益且边界通过后接线并默认启用。
