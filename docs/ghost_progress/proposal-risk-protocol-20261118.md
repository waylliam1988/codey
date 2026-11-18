# 条件接受率与纠正成本消费者：运行前冻结

2026-11-18，`DESIGN_FROZEN`；冻结提交之后才运行一次矩阵。
实现：`tests/manual/ghost_memory_routing/proposal_risk_trial.py`；
契约：`tests/test_proposal_risk_trial.py`。

## 消费者、变量与依赖

消费者根据同项目同情境下过去明确接受/拒绝方案的经历，决定推荐哪个新方案，
或者先澄清。新增变量是**未来接受概率及拒绝后的纠正成本**，不重复已确认的当前目标，
不复述验证事实，也不重跑讨论句序候选。比较同一条件计数和概率计算投影，
不声称新的统计学习算法。预测本身不能成为用户事实、权限、evidence_refs或完成依据。

事件 → Laplace预测 → 真实主调用建议 → 后续明确评估 → 概率复盘及下一阶段计数更新。
每条预测包含scope、情境、策略、方案、依据ref、概率、created及due。
实际模型建议只有一个，候选方案来自固定任务，不能作为多候选生成收益。
预测和原始经历、计数分别走独立ContextSource及现有admission。

生产若通过，须有正常项目入口中的有限方案输入与显式评估信号（真实run/ref/完整回复digest），
并完成来源权威、用户关闭、删除/撤回/重建/重启/并发恢复/replay和模型配置限制。
当前仅实验入口；现有项目确认目标不自动变成方案接受率。
不提前添加无人消费的生产模块，也不把实验的synthetic_clock_snapshot当生产权威。

## 样本、时间与留出

20条独立**合成经历流**，10对相同当前问题、相反个人历史。
field_storage与team_storage两个情境；local_journal/shared_service两种策略。
每人一个情境的偏好与另一情境相反，初始奇偶两类各10人。
所有模板、种子、方案成本和后续评估已由脚本常量冻结；无live开发样本、选参或追加样本。
统计单位为经历流，240个请求不是240个用户。

每个情境/策略17–21条历史，偏好策略接受数为n-(4+scenario%3)，
相反策略接受数互补；固定SHA256排列防止全正前缀。68–84条初始历史为
**预制合成历史回复与明确评估**，不是额外实机请求，不冒充真实用户数据。

三个阶段分别在模拟时刻1000/2000/3000查询，情境为field/field/team。
每阶段两新方案的benefit、correction_cost、ASK成本和稳定平局顺序由脚本定义。
每阶段所有四臂返回以后，phase0/1分别有两条明确评估；phase2无反馈。
后续评估独立使用固定SHA256抽样：真实偏好策略概率78%，另一策略22%，
**不使用模型回答或后验概率生成反馈**。真实偏好仅用于这些留出抽样，不进入输入。

评估的对象是本次任务列出的两个方案，不要求模型实际推荐两者；ASK同样可得到
对任务方案的明确评估。合成评估关联forecast臂的实际交互ref、完整summary SHA256、
scope、情境和时间。三种有历史臂共享这一已发生轨迹，属于反事实对照，
不冒充四个独立在线用户。明确评估自身更新计数，与预测是否命中无关。
发送前持久保存预测及依据；当前阶段反馈不泄漏到当前输入。

## 四臂与评分

20流×3阶段×4臂=240次物理生成，按(stream+phase)%4轮换：

1. none：无个人历史、计数或预测；共同缺省选择ASK。
2. retrieval：相同完整历史，模型自行按共同策略计数、计算及选择。
3. counts：相同完整历史，加四组条件接受数/总数；简单统计主基线。
4. forecast：相同完整历史、相同计数，再加两方案的概率和期望效用；不直接给推荐标签。

共同策略p=(accepted+1)/(total+2)，效用p×benefit-(1-p)×correction_cost，
ASK效用=-ask_cost。精确分数算术定义正确动作，平局优先ASK再第一方案。
summary仅strip首尾空白后须逐字等于正确标签，额外说明、围栏或标点都记错。
所有臂按同一份合法个人历史的正确动作评分，none缺少该信息；
配对问题完全相同但个性化正确动作不同，不能用通用指令遵循替代个体收益。

这个动作指标是**固定决策策略的数学代理**，不是实测用户满意度；
也不按偶然的未来单次接受/拒绝倒推最优动作。未来预测质量另外用Brier评分。
每条预测分别在完整、明确、匹配的评估中判hit/miss，预测p≥0.5对应接受；
80条可判定、40条unjudged，共120条review。没有观测、隐含评价、空/错误绑定、
过期或退役来源均unjudged；不将沉默、服务故障或日志缺失记作拒绝。
时间须满足created≤behavior.at<event.at≤due，due=created+5。
合成边界只接受固定明确评估语法，不声称支持自由文本自动理解。

## 模型、预算、成本和停止规则

唯一目标：koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M，
loopback http://127.0.0.1:5001/v1，OpenAI Completions/native tools，temperature0，非流式。
window/output/safety/keep_recent=262144/8192/24576/32000，input_limit=None，timeout120秒。
复用上轮CapturedProvider，正常shared kernel、durable边界、managed output、native done、
physical Attempt Gate，max_turns1、仅control。无独立Ghost生成、第二遍回答、embedding或HF。

历史24000字符、计数1400、预测1600；超限或截断停止，不改变预算。
每格仅一个物理请求；非空完整的错误输出照常记错。
空/不完整回复、缺usage、服务故障、缺格/缺review立即保存INCOMPLETE并停止，
不重试、不重启服务、不追加有利样本，不计阴性。
开始/结束只读models核对与loopback契约测试不计生成请求。

收益门必须全部满足：forecast三阶段全对流≥18/20；比counts和retrieval各多≥4流，
比none多≥8流；每阶段≥18/20；初始奇偶类各≥9/10；80条Brier均值≤0.25。
成本门：每格比counts额外输入≤600tokens；forecast p95≤counts p95×2+1000ms；
每流派生JSONL≤128KiB，20流及完整240格、120review与usage全部齐全。
持平不算增益，不更换主基线、scorer或门槛；阴性终止本候选，不换提示再跑。

记录每格input/output tokens、物理请求、wire大小/hash、准备耗时、总耗时，
以及冷读/预测的shared_state_preparation_ms。非流式visible_ready_ms为kernel返回时间，
不能冒充网络首token或流式体验。派生JSONL与全部SQLite/wire/原始回复/审计磁盘成本分开报告。
留出抽样仅验证合成预测，不能宣称真实用户校准或长期满意度收益。

## 验证与留存

```powershell
python -m pytest tests/test_proposal_risk_trial.py tests/test_semantic_goal_trial.py tests/test_interaction_feedback_trial.py -q
python -m tests.manual.ghost_memory_routing.proposal_risk_trial --output .e2e-artifacts/proposal-risk-20261118-live --live
```

本地`.e2e-artifacts/proposal-risk-20261118-*`保存完整回复、wire、SQLite、state、原始诊断。
仓库只保存复现脚本、契约、协议及脱敏指标。五个全树静态门、累计owned的
三检查器与额外Any审计完成后收尾；未修改依赖的原始非零诊断独立保存、明确报告。

冻结前合跑155项通过，全仓Ruff通过；两个本批文件Pyrefly/ty零错误，额外mypy
Any审计owned零诊断。完整mypy命令exit1，11995项/333个未修改依赖诊断原样留存，
不称完整专项命令通过。此前测试Optional声明错误已在冻结前修复。
