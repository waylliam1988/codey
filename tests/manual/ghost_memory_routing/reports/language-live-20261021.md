# Ghost language live/off evaluation — 2026-10-21

- suite：`language_live_20261021`
- commit：`3d79ca9c985da899ff4f6274091fdb80ab4cbe7d`
- model：`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`
- endpoint：`http://127.0.0.1:5001/v1`
- context / reserve / keep recent：262144 / 32768 / 32000
- tasks / cells / reps：language 20 tasks；`lang_on_d2` 与 `lang_off_d2`；各 1 次
- 对照：同一任务序列、同一模型和配置，仅切换 language Ghost treatment

## 结果

| cell | runs | done | judge pass | judge fail | chars avg |
|---|---:|---:|---:|---:|---:|
| `lang_on_d2` | 20 | 20 | 20 | 0 | 596.2 |
| `lang_off_d2` | 20 | 20 | 0 | 20 | 1118.9 |

40 个 live 请求全部正常完成，没有 KoboldCpp 超时、格式错误或审批挂起。脱敏指标见
[language-live-20261021.json](language-live-20261021.json)。

## 当前生产边界

- 是否经过真实 provider-send：是；实际 task entry、builder、kernel、ApiProvider 和 KoboldCpp。
- 是否改变默认 prompt：实验只切换已有 Ghost treatment，不修改默认模板。
- 支持的模型 / 任务 / 配置：一个 KoboldCpp modelA、language family、当前 `lang_*_d2` 任务集。
- 生产合并结论：`SHADOW_ONLY` / 条件候选；没有改变默认启用，也没有跨模型推广。

## 限制和阴性结果

结果只能支持当前 modelA/config 的条件候选，不能推广到第二模型、command、留出任务或默认
language 晋级。未测成本、延迟和 token usage；字符数只作脱敏规模指标，不是 token 消耗。
没有提交原始 prompt、reply、state 或完整日志。

## 下一步

先把这组结果接入 0.6.0 冻结 evaluator 的正式记录，再在 0.6.1/0.6.2 admission、控制、
reset/restart 和恢复矩阵稳定后扩展第二模型或留出任务。World Model 继续保持 `SHADOW`。
