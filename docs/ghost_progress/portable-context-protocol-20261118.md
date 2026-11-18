# 已确认上下文通用化：实机冒烟协议

运行前冻结。目标是检验正常项目入口消费本地上下文时，输出预算和流式不再构成人为禁用条件。
复用已经完成的中文、项目经历与语义目标效果证据，不重跑原效果实验，也不把本批当成新的统计优越性证明。

- 一个合成用户/项目，固定经历：助手选择 opalpanel，理由 offline_cursor_sync；
  用户原话支持 warm 语气和非存储类 keyboard accessibility 项目目标。
  来源通过正常输入队列、Chat 任务和确定性 fixture transport 记录，人工提案并确认；种子不是实测用户。
- 模型固定 Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M，
  `http://127.0.0.1:5001/v1`，Chat Completions、native tools、temperature=0。
  四种配置：(128, 非流式)、(8192, 非流式)、(4096, 流式)、(8192, 流式)。
  上下文262144、reserve32768、keep32000。模型前后元数据留本地。
- 每配置三个臂：admission关闭、开启、开启且当前用户明确覆盖目标/语气。
  共12次真实生成；每格独立本地状态，同样种子，新会话重启后进入正常项目 writer。
  每格只允许一次生成、一次 done；禁止额外 Ghost 生成、HF、embedding、第二模型评分、重试和修复回答。
- 固定提问要求 done.summary 为 JSON，choice/reason/priority/tone 为短标识。
  开启臂须恢复 opalpanel/offline_cursor_sync、keyboard/warm；覆盖臂改为 animations/technical。
  未知值合法，但记为相应未命中；不从格式错误中猜答案，不事后改 scorer。
- 保留物理请求、终端回复、usage、state和失败信息在 `.e2e-artifacts`。
  检查每格物理请求数、来源实际发送、终止状态、明确反馈覆盖、tokens和耗时。
  精确来源全有/全无及单次调用是边界门；模型字段准确性是描述性冒烟结果。
- 服务失败或首个不完整/格式失败即停，记录 INCOMPLETE；不冒充实验阴性或通过。
  这只有一个合成样本，不能声称所有模型、所有任务或自然语言温暖度有收益。
  不以同一例子的四种配置当作四个独立用户样本，也不推断128/8192是产品必要阈值。

复现：

```powershell
python -m tests.manual.ghost_memory_routing.portable_context_trial --output .e2e-artifacts/portable-context-20261118-live
```

离线生产边界由独立 HTTP/SSE 正常入口回归覆盖：不同模型名、Chat Completions/Responses、
128/4096/8192、跨会话/跨项目用户偏好、项目隔离、待审与已确认目标、纠正、多目标预算、
删除/撤回/禁用/重启/冷重建/发送前源校验。Research 的补充来源还需在实际 builder 边界验证，
不把 builder 测试写成完整研究报告质量实验。

运行器修正记录（生成目标、矩阵与评分不变）：首次启动在输入队列缺失 run_id 处停止，
0次生成；第二次第一格模型已正常完成，但正常入口重绑 usage observer 导致脚本外接收集器为空，
批次按协议停止，未计通过。两次目录与 failure.json 保留。
修正为在 provider.bind_usage 边界同时保存自身记录并转发生产 observer，新增原始 reply 留存。
完整矩阵在独立 v3 目录重开，不能拼接前两批为通过，也不能把该观测器错误称作模型服务失败。

用户后续明确授权一次 KoboldCpp 故障重试：v3 第6格返回嵌入 HTTP 头的畸形正文，
原始失败及未知 usage 保留；前5格不重跑。只对第6格以新任务/独立本地状态补一次请求，
记录在该格 `authorized-retry-1` 子目录，然后继续剩余6格；后续故障不自动重试。
这是用户授权的协议例外，最终如完成标记 `COMPLETE_WITH_AUTHORIZED_RETRY`，
不能称原协议无重试完成，也不能把失败请求用量计作0；评分与变量不变。

```powershell
python -m tests.manual.ghost_memory_routing.portable_context_trial --output .e2e-artifacts/portable-context-20261118-live-v3 --resume-after-authorized-retry
```
