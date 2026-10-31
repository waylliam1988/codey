# Gemma 4 12B Research 收敛实施记录

2026-10-31，基于 `060656db` 和 [诊断方案](research-convergence-diagnosis-20261031.zh-CN.md) 逐项修改、验证、提交。
用户已授权使用当前本地 Gemma 4 12B 做真实验证；每个问题单独提交，保留原始输出。

本地 `/v1/models` 确认部署为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`。
小模型成功是有价值的工程证据，不自动证明另一模型一定成功。

## 1. 评审输入去重与完成动作收敛

`task_working_context` 不再为评审重复整个 task/basis。评审提示沿用冻结工具契约，只要求提交 assessment，
移除该阶段不适用的 workspace 验证和普通 done 提示。没有改变执行循环、回执或发布条件。

验证：评审提示、评审范围及原生 codec 共 48 项测试通过，修改文件 Ruff 通过。
提示测试覆盖原生/文本与请求级契约两种组合，检查唯一 basis 和实际提交动作；尚未据此宣称模型语义通过。

## 2. 取消强制生成反证段

报告允许 `counter=[]`。编译器生成明确的本轮记录状态，且不把这句话当作来源事实送给语义评审。
有实际限制或反证时仍须正常提供支持；问题缺口继续保留在 coverage。

验证：空 counter、真实 finding 编译、引用覆盖与持久化语义评审共 37 项通过；修改文件 Ruff 通过。

## 3. 已读 passage 跨问题采用与来源解析

`save_finding` 可直接选择本轮已读的精确 passage，由该快照确定来源；不再要求模型重复填写 source_ref。
显式传入的 source_ref 仍作为断言校验，旧冻结调用使用同一写入实现，不产生第二套 writer。
同一 passage 可为另一问题保存/采用；相同 claim 的知识身份和修订复用，新的 fN 仅表示问题采用关系。
原读取回执的 question_ref 不变，不伪造第二次调查。coverage 继续拒绝把其他问题的 aN 当作本题实际调查。

验证：相关选集 48 项通过，Ruff 通过。另有 evidence_split 的两项旧夹具失败，已在独立
`e4334c24` 基线 checkout 复现相同失败（原文不含所写引文、未提供显式 evidence）；没有放宽来源校验。
跨问题测试实际执行一次读取、两次采用、回执恢复，确认没有新增知识修订或伪造读取。
