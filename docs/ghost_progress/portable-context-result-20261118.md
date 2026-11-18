# 本地上下文通用化：实机冒烟结果

状态 `COMPLETE_WITH_AUTHORIZED_RETRY / NORMAL_ENTRY_SMOKE_ONLY`。
[冻结协议](portable-context-protocol-20261118.md)，初始提交`acad46df`，
用户授权重试修订`c54e711d`；[脱敏指标](../../tests/manual/ghost_memory_routing/reports/portable-context-20261118.json)。
本记录只描述实验；当前产品范围见[生产接线记录](portable-context-production-20261118.md)。

## 实际运行与结论

一个合成经历，四种配置×关闭/开启/当前指令覆盖，共12格完成。
正常 App 输入队列 → 新会话项目 writer → 本地来源与 admission → 真实 HTTP。
模型 Gemma4-12B，Chat Completions/native、temperature0；非流式output128和8192、
流式output4096和8192，上下文262144/reserve32768/keep32000。
种子通过正常 Chat fixture 记录，不产生实机生成；每个消费格一次正常生成，Ghost额外生成0。

| 冻结字段指标 | 关闭4格 | 开启4格 | 当前指令覆盖4格 |
|---|---:|---:|---:|
| choice+reason均精确匹配 |0|0|0|
| priority精确匹配既有目标/当前覆盖 |0|4|4|
| tone精确匹配warm/technical标签 |0|0|4|

准确率没有事后改分。开启臂给出的实际choice字段是opalpanel，reason字段是
`offline_cursor_sync preserves the project interface cursor`；tone字段是`warm and considerate`。
它们包含预期信息，但不等于冻结scorer要求的短标识，因此两个严格指标仍计失败。
这暴露了短标签接口的局限，不能拿自然语言复述改成通过，也不能把它解释成已证明自然陪伴语气质量。

可以支持的窄结论：这个合成项目中，普通非存储目标在大输出预算及流式路径被消费；
当前用户明确改要求时，四种配置都选择当前要求。原始经历和偏好的实际发送在开启/关闭两臂正确切换。
这不是新检索/学习机制的收益证明；复用历史独立实验，不声称全模型、全任务等效收益。

## 失败、重试与成本限制

- 初次运行因队列缺run_id在生成前停止；第二次第一格正常完成，脚本usage observer被生产绑定覆盖，
  记录失败且不计通过。目录分别保留，代码修正未改变任务和scorer。
- v3前5格完成，第6格KoboldCpp正文混入HTTP头，客户端没有自动重发；用量未知。
  用户随后明确授权一次重试，以新任务和独立状态记录在该格`authorized-retry-1`，再完成剩余6格。
  v3共13次物理尝试、12份完整回答、1份服务失败。不能称无重试完整矩阵。
- 六个流式回复未上报tokens，保留null，不能计0或宣称完整成本门通过。
  非流式完整回复报告输入约5581–5907、输出36–48tokens；各完成格约4.1–4.9秒，
  这些是本次总完成耗时，不是首token延迟或统计性能结论。

本地证据：`.e2e-artifacts/portable-context-20261118-live*`。
v3保留models前后/恢复快照、progress、failure、授权恢复说明、逐格request/reply/terminal/usage和state。
仓库只保存脚本、协议及脱敏指标。两个早期运行和故障尝试均保留，不拼接为无故障通过。

下一步：如要测自然语气质量或精确短标签服从，应另冻对应评估；不更改本批scorer，也不要求为了
允许用户携带自己的已确认上下文而重新绑定每一个模型名或输出token数。
