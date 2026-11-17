# Ghost 实验：Language Live Evaluation 2026-10-21

- 实验名称：Language Live Evaluation 2026-10-21
- 状态：`EXPERIMENT_COMPLETE`（受测中文子集已生产启用）
- 负责人：Codex
- 实验 commit：生产接线 `3d025cce`；评估运行 commit `3d79ca9c985da899ff4f6274091fdb80ab4cbe7d`
- 关联 Roadmap 验收门：0.6.0 评估冻结；0.6.5 language shadow 的前置证据
- 生产合并结论：`PRODUCTION_MERGED`（2026-11-17，受测模型普通 Chat 中文子集）

2026-11-17 晋级要求修订：一个本地模型及配置的行为收益与相关生产边界通过后，即可在受测范围
正式启用，无需第二模型。本实验已有单模型行为对照；受测中文子集现已[生产接线并默认启用](language-production-enablement-20261117.md)。
下方历史测量及所链接报告保留当时结果；报告中的第二模型后续计划不再是本模型生产晋级前置。

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
- 是否改变默认 prompt：历史实验只运行 on/off treatment；2026-11-17 起普通 Chat 在受测模型及配置下默认投递已确认中文偏好。
- 支持的模型 / 任务 / 配置：单一 modelA、普通 Chat 的中文 language 投递；具体模型、协议和预算见[启用记录](language-production-enablement-20261117.md)。
- 生产合并结论：`PRODUCTION_MERGED`（仅上述受测子集）；历史报告原文保留。

## 限制和阴性结果

结果支持当前 modelA/config、language 任务范围；中文子集已接入普通 Chat 并默认启用，
不要求第二模型。不能推广到未测试模型、command 或未验证任务范围。历史实验没有 token usage、成本、延迟或
跨模型数据；字符均值只是脱敏规模指标。本次规则修订不补足这些缺失证据。

## 下一步

本次已根据既有单模型结果完成受测中文子集的生产接线与默认启用。按用户要求，停止新增实机 A/B，
不继续补跑；新增批次的不完整结果单独保留在启用记录中。第二模型和留出任务作为未来独立扩展，
不阻塞此次晋级。World Model 继续 `SHADOW`。
