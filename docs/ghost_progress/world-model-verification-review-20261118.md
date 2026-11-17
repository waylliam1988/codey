# World Model：验证结果的预测复盘消费者

- 日期：2026-11-18
- 状态：`INCOMPLETE`（本节先冻结，未执行收益实验）
- 关联：0.6.12；基线 `bc9b977910937fef2566f17cba4bf67fa494493c`
- 生产状态：未晋级；不新增 runtime 空壳或默认关闭的生产模块。

## 假设与消费者

现有 completion decision 已能按真实退出状态及有界诊断区分环境故障，不能把该识别能力
重新计为 World Model 收益。本候选只检验：一个对前次 verification 的有界预测，经下一次
真实 verification 复盘后，是否能帮助本地模型更准确选择下一步，并降低把旧成功或旧失败
当作当前事实的比例。消费者是验证结束后的只读复盘建议；模型回答不触发工具、权限或完成。

闭环为已提交 verification/proof ref → 预测下一次同命令的结果类别 → 只用后续真实
verification/proof review → 有 TTL 的有界 projection → 独立 ContextSource、PromptEnvelope
和现有 admission → 本地模型复盘建议 → 固定 action scorer。没有后续有效结果、workspace
身份不匹配或来源不可用时保持 unjudged，不猜 hit/miss。预测自身不得写入 evidence_refs、
CompletionProof 或权限。生产接入前还必须补普通入口和完整相关生命周期，实验直接发送
不冒充已通过生产 Attempt Gate。

## 冻结矩阵

主要变量仅为 reviewed World Model projection 是否投递。off 仍有相同真实 verification
结果、当前 proof 状态及现有 runtime 指导；on 不能删除不利事实或给 scorer 直接塞期望答案。
每题独立空历史 provider，交替 on/off 顺序，保留原回复、延迟及 provider 使用量于本地。

- 模型：koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M。
- 服务：http://127.0.0.1:5001/v1；openai-completions；temperature=0；不启用工具执行。
- 预算：context 262144、reserve 32768、keep_recent 32000、output_tokens 256、timeout 120s。
- 五类任务：重复环境失败；环境修复后验证成功；真实产品验证失败；缺少后续 verification；
  旧成功但 workspace 已变。每类四种固定问法，共 20 题/arm，on/off 共 40 次；不补跑语言实验。
- 期望 action 分别为 inspect_environment、report_verified、inspect_source、verify、verify。
  只接受一个 JSON action；非法结构、额外未知 action、截断均为 scorer 失败，不用第二模型裁判。
- prediction/review/projection 各自至多有界 refs/digests/reason codes，不持久化 raw 输出。
  投递字符预算最多 900；所有维护/TTL/review 使用显式 as_of。
- 离线先验证真实命令结果进入 proof、hit/miss/unjudged、身份绑定、replay/digest、scope、
  过期/撤回/删除/reset/重开/输入损坏，以及 admission 的关闭、预算与逐字节投递。

晋级门：两 arm 全部完成；on 正确 action 至少比 off 多 3/20，且五类逐类不退步，
无把 unjudged 当已验证、跨 scope/过期泄漏或跳过验证的安全回归，投递不截断。
样本只支持该模型/config 与任务，不声称跨模型或统计普遍性。过门后补齐真实生产消费者、
正常入口、用户关闭/隔离/回滚和未测模型基线回退，再默认启用受测范围。

停止条件：模型 ID 不匹配、服务错误、空/截断响应、缺对照或 fixture 无效时停止并保留
INCOMPLETE 与失败证据，不能计为模型阴性；完整矩阵无净收益则记录负结果并终止本候选，
不将无消费者模块留在生产。任何阈值修订都必须在另一个实验中重新冻结。

## 当前依赖与限制

可复用：Hebbian 解码及恢复、三 scope 控制、删除 Manifest 默认调度、普通语言 App→HTTP
与现有 ContextSource/admission 验收；这些不证明本候选已接线。整个 G0 尚未闭合：稳定
排序/digest、记录型 control payload、完整硬退出/跨 run 发送恢复、混版本及磁盘策略仍需审查。
本候选先做隔离只读实验，生产晋级只能按本消费者实际涉及的边界逐项证明，不能沿用
“已有测试”把缺口写成通过。
