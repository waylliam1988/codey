# 流式用量与明确标签接口：修复及定向复验

日期：2026-11-18。状态：`FROZEN / NOT_RUN`，实机前提交本协议。
本轮仅修实验脚本和验证接口，不修改生产逻辑，不实现开放式偏好学习。

## 问题与证据

历史[12格冒烟](portable-context-result-20261118.md)的请求文件证明：六个流式格没有
`stream_options: {"include_usage": true}`。脚本直接构造`ApiProvider`，漏接正常Local连接的
`configure_usage`。历史用量必须继续保留null；不能据此断言KoboldCpp不支持流式用量。
本轮复用生产的配置函数，保留原usage解析器，并逐事件保存解析前的JSON到本地
`response-events.jsonl`，核对用量解析器的输入和应用记录。若无usage事件，仍需区分
服务未返回与更早的传输解码问题，不能仅凭应用记录缺失裁定服务故障。

旧提示只要求短标识，没有明示reason/tone的合法值；输入包含完整短语，scorer却要求
`offline_cursor_sync`和`warm`。原分数保持不变；该接口混入了格式遵循，不能单独判断
经历恢复，更不能以tone字段判断自然温暖度。本轮只修明示标签的接口，不宽松匹配短语。

## 冻结配置与指标

- 入口：正常队列→新会话项目writer→独立来源/admission→实际HTTP；同一合成经历和手动确认状态。
- 模型：`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，
  `http://127.0.0.1:5001/v1`，Chat Completions、native tools、temperature0。
- output8192、context262144、reserve32768、keep32000；两配置仅stream不同。
  每配置admission关闭、开启、开启且当前用户覆盖三臂，共6次生成，每格独立state。
- 六格共同使用`LABEL_TASK`，明确四字段枚举及`warm and considerate`对应`warm`。
  choice/reason各有干扰值，列表不能充当历史证据。关闭臂预期unknown/neutral，
  开启臂预期opalpanel/offline_cursor_sync/keyboard/warm，覆盖臂改animations/technical。
- 严格检查四个字符串字段、无额外字段、枚举完全相等；分别报告choice、reason、
  二者联合召回、priority、tone标签、关闭臂unknown和format。旧三个主评分条件不变。
- 流式请求必须包含include_usage；单独检查原始usage事件、应用usage记录、输入/输出tokens。
  缺失保留null，不能补估为真实用量；完整用量目标为6/6上报。只报告本次耗时，不推断性能提升。
- 每格只允许一次物理生成；Ghost额外生成0，不用HF、embedding或第二模型。
  单次请求、来源实际全有/全无、done完整为边界门；字段命中只是一个合成样本的描述性指标。
- 服务错误、响应不完整、非法JSON/标签格式首错即停，保存`INCOMPLETE`，不自动重试。
  字段选错但格式合法继续记录；用量缺失也保留并完成其余格，不把缺失称为通过。
- 停止/完成后不更改任务、枚举和scorer，不与历史数据拼接，不扩大样本或重跑其他预算。

这次同时修复两个不同子系统，不能从合并改动推断单项行为收益；HTTP/SSE回归独立验证
用量配置和usage-only终末事件消费。明确枚举帮助回答的效果只归于接口改善，不归于Ghost学习增强。

## 复现

```powershell
python -m pytest tests/test_ghost_portable_context_trial.py -q
python -m tests.manual.ghost_memory_routing.portable_context_trial --protocol labels-v2 --output .e2e-artifacts/portable-context-labels-v2-20261118
```

原始请求、事件、回复、usage、state和模型快照留本地；仓库仅保存脚本、协议、脱敏结果。
历史复现用原协议冻结提交`acad46df`及授权恢复修订`c54e711d`；当前legacy模式保留旧问题/评分，
但已补用量请求并新增分项诊断，不能冒称原始网络字节完全相同。

## 结果

待按冻结协议执行。测试命令、实际结果和本地模型可用性在后续独立结果提交记录。
