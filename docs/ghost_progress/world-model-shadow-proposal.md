# World Model shadow 研究建议

- 实验名称：World Model bounded prediction review
- 状态：`SHADOW`
- 负责人：Codex
- 最后更新：2026-10-21
- 关联 Roadmap 验收门：0.6.12 World Model review/shadow context
- 实验 commit：无（设计提案，尚未实现）
- 生产合并结论：`SHADOW_ONLY`

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

## 实验配置（待开始）

- 模型 / suite / endpoint：先离线 deterministic fixture；需要模型时另行记录
  endpoint、model ID 和上下文长度；不复用 admission smoke 的 20/20 作为 World
  Model 证据。
- tasks / cells / reps：至少覆盖 task completion、provider failure recovery、
  verification correction 三类任务，每类保留 no-model replay 和 review consumer
  对照；最低样本量、scorer 和成本预算在实现前冻结。
- on/off 对照：无预测 projection、shadow projection 但不发送、显式 review
  consumer；不得把 shadow 线直接加入默认 prompt。

## 当前阻塞和边界

- 当前仓库没有可证明的生产 World Model consumer，因此不启动模型实验，也不新增
  默认 prompt section。
- 需要先完成 0.6.2 的 scope/expiry/restart/admission 矩阵，再决定是否实现
  bounded prediction record；否则无法证明删除、过期和重启后不复活。
- 原始 prompt/reply、模型输出和完整 state 仍只留本地归档；未来仓库只保存
  deterministic fixture、脱敏指标和负结果。

## 下一步

先完成 Ghost 0.6.2 控制生命周期和真实 outbound bytes 对齐；随后为一个明确的
review 消费者写最小 offline fixture。若 fixture 没有生产消费者或指标收益，直接
标记 `ABANDONED`，不进入 runtime。
