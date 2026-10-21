# Ghost 实验：Language Live Evaluation 2026-10-21

- 实验名称：Language Live Evaluation 2026-10-21
- 状态：`EXPERIMENT_COMPLETE`（单模型条件候选）
- 负责人：Codex
- 实验 commit：生产接线 `3d025cce`；评估运行 commit `3d79ca9c985da899ff4f6274091fdb80ab4cbe7d`
- 关联 Roadmap 验收门：0.6.0 评估冻结；0.6.5 language shadow 的前置证据
- 生产合并结论：`SHADOW_ONLY`

## 本次完成

在全新隔离 state 和当前 KoboldCpp modelA 上完成 language on/off 各 20 次：on 为 20/20
judge pass，off 为 0/20；40 次 provider 请求全部正常完成。原始 prompt/reply、state 和完整
日志只保留本地 `.tmp-ghost-language-live-20261021`，仓库只保存脱敏报告。

## 验证命令和结果

详见 [live/off 报告](../../tests/manual/ghost_memory_routing/reports/language-live-20261021.md)
和 [脱敏指标](../../tests/manual/ghost_memory_routing/reports/language-live-20261021.json)。

## 实验配置

- 模型 / suite / endpoint：`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M` /
  `language_live_20261021` / `http://127.0.0.1:5001/v1`
- tasks / cells / reps：language 20 tasks；`lang_on_d2`、`lang_off_d2`；各 1 次
- context / reserve / keep recent：262144 / 32768 / 32000
- on/off 对照：同任务、同配置，仅切换 language treatment

## 当前生产边界

- 是否经过真实 provider-send：是，真实 KoboldCpp。
- 是否改变默认 prompt：不修改默认模板；仅运行已有 on/off treatment。
- 支持的模型 / 任务 / 配置：单一 modelA、language family、当前 d2 任务集。
- 生产合并结论：`SHADOW_ONLY`；不改变默认启用。

## 限制和阴性结果

结果只能支持当前 modelA/config 的条件候选，不能推广到第二模型、command、留出任务或
默认 language 晋级。没有 token usage、成本、延迟或跨模型数据；字符均值只是脱敏规模指标。

## 下一步

把结果接入 0.6.0 frozen evaluator 的正式记录；在 0.6.1/0.6.2 admission、scope、reset、
恢复和并发矩阵稳定后，再按最弱必测模型扩展第二模型或留出任务。World Model 继续 `SHADOW`。
