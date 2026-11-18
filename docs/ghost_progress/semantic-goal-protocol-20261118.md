# 项目目标纠正与新方案选择：运行前冻结

2026-11-18，`DESIGN_FROZEN`。实现为
`tests/manual/ghost_memory_routing/semantic_goal_trial.py`，契约为
`tests/test_semantic_goal_trial.py`。冻结提交后才进行一次实机矩阵。

## 消费者与新增变量

用户明确确认/纠正项目的设计优先级后，新会话将其用于**此前未出现的新方案选择**。
与已终止的讨论句序实验不同，本批测语义取舍（离线使用/同时协作），不是相同句子的重排；
也不同于两个复述当前验证事实的旧 World Model 候选。正常输出预算8192是另一个明确新变量。
不重新评分或追加旧实验样本，不扩大原output128经历消费者的生产边界。

第一候选是简单的、人工确认的当前目标：合法scope中最近的明确确认优先，后续确认修正或维持。
这是可验证写回信号，不是自动偏好提取，不以模型自己的回答作为用户理解的真值。
第二候选将目标与新方案属性结合，预测下一条明确用户评估会指出哪个方案适合当前目标。
预测含对象、情境、来源、时间窗、未校准confidence0.6与上次review，经独立有界ContextSource发送。
预测不是事实、任务权限、evidence_refs或完成依据。无独立生成、embedding或HF模型。

## 冻结样本、时间与对照

20条独立**合成**个人/项目经历流，10组相同当前问题配相反初始目标；离线/协作各10条。
10个域名和全部方案由脚本中的固定模板定义，三个阶段各有新的方案标签；方案属性互补，
每阶段交替位置。全部为预定留出矩阵，无live开发样本、选参或后验有利样本。
统计单位20条经历流，240次请求不能算240个独立用户。

| 阶段 | 模拟时间 | 发送前已知 | 所有四臂回答后才出现 |
|---|---:|---|---|
| 初始 | 10 | 人工确认的初始长期项目目标 | 明确改为相反目标，并指出本阶段适配的方案 |
| 纠正后 | 20 | 以上反馈及实际候选回答 | 明确维持已纠正目标，并指出本阶段适配的方案 |
| 维持后 | 30 | 以上两次可归因反馈 | 无反馈；review保持unjudged |

三阶段四臂共**240次物理生成**，每格一次，按`(stream+phase)%4`轮换顺序：

1. `none`：没有相关历史、目标状态或预测；共同缺省选第一方案。
2. `retrieval`：相同已发生经历，含原用户确认、实际候选回答及之后用户原话。
3. `confirmed`：完全相同历史，加人工确认的当前目标；不是新的学习算法。
4. `forecast`：完全相同历史和确认状态，再加对新方案的用户评估预测。

相同问题、位置、权限、主调用出口及预算；三种有历史臂共享同一份已发生候选交互轨迹，
是共享轨迹的反事实对照，不冒充各臂独立在线用户。初始确认及反馈为预定合成信号日程，
不因回答正确而改写用户。只有forecast臂的实际回答作为共同轨迹的被反馈对象，
四臂完成前不生成或保存当前阶段反馈，未来反馈不能进入过去输入。

绑定实际reply/run ref、完整summary SHA256、scope、context和时间后才接受明确人工确认。
确认本身可更新当前目标，与预测是否存在/是否命中无关。预测比较下一条明确评估的方案标签，
反馈须在`created <= behavior.at < feedback.at <= due`内，due=created+5模拟单位。
缺反馈、非明确确认、错身份/摘要/scope/context/时间或空回答保持unjudged；不把沉默、
技术失败或日志缺失当负反馈。初始目标反转导致的miss是需求改变，不当然是过去回答有错。
永久目标无TTL；预测有时限，本批不声称临时目标过期已生产验收。

## 模型、发送与测量

唯一目标`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，
loopback `http://127.0.0.1:5001/v1`、OpenAI Completions/native tools，temperature0，非流式。
window/output/safety/keep_recent=262144/8192/24576/32000，input_limit=None，timeout120秒。
shared kernel、真实durable操作/managed output/native done、physical Attempt Gate，max_turns1，
仅control，不修改生产profile。本批仍是实验入口，普通App/项目入口与生命周期另验。

历史3200字符、确认状态600、预测900，截断直接INCOMPLETE；三臂原始历史逐字相同。
只strip外侧空白后，summary必须与正确方案标签逐字相等；不剥围栏、不修回复、不调用第二遍。
完整非空但错标签/格式记错。空/截断、缺usage、不完整配对、服务故障停止，保存失败证据并
记INCOMPLETE，不计阴性、不重试、不重启服务。开始/结束只读models核对不算生成。

每格报告input/output tokens、一次物理请求、wire bytes/hash、准备耗时、kernel总耗时，
以及外层冷读/状态选择/预测的shared_state_preparation_ms。非流式可见时间是kernel返回，
不是网络首token；不宣称流式体验。派生JSONL状态与完整SQLite/请求/审计磁盘成本分开报告。

## 冻结收益与成本门

主指标是每流**三个阶段全部正确**；另报告逐阶段、初始两类、逐格正确与格式有效数。

人工确认目标候选同时满足：

- 全对流≥18/20，每阶段≥18/20，初始两类全对流各≥9/10。
- 比none多≥8/20全对流，且不低于相同历史retrieval。
- 每格相对retrieval额外输入≤250tokens，p95≤retrieval p95×2+1000ms。
- 每流派生JSONL≤64KiB，20条流/全部240格/usage/review完整。

这个门只支持“确认目标及相同经历改善未来行为”的产品连续性，不要求胜过原始检索。
如果与retrieval相同，必须明示没有额外准确率或学习机制收益，不能声称压缩优势。

World Model候选独立要求：同样的18/20、每阶段18、各初始类9，且全对流同时比
confirmed和retrieval各多≥4/20、比none多≥8/20；每格相对confirmed额外输入≤400tokens，
p95≤confirmed p95×2+1000ms，状态及完整性门同上。与confirmed持平即没有预测增益，终止此候选。

不换主基线、scorer、门槛或样本量，不因阴性换提示再跑。有收益的候选继续完成普通入口、
真实来源权威、用户关闭、撤回/删除/重建/重启/并发恢复/replay，受测模型/config默认启用；
未过边界不能标PRODUCTION_MERGED，无收益不提交无人消费的生产模块。

## 命令与留存

```powershell
python -m pytest tests/test_semantic_goal_trial.py -q
python -m tests.manual.ghost_memory_routing.semantic_goal_trial --output <新的本地目录> --live
```

原始回答、wire、SQLite、JSONL和完整诊断留`.e2e-artifacts/semantic-goal-20261118-*`本地；
仓库仅脚本、契约、协议、脱敏指标与记录。每轮相关回归、五个全树静态门和owned额外Any审计。

冻结前：新契约47项通过；与旧反馈实验合跑92项通过；ruff全树通过；两个owned文件
mypy、Pyrefly、ty常规零错误，额外mypy Any审计owned零诊断。后者完整命令exit1，
未修改依赖11995项/333文件，原始日志保留，不以过滤输出冒充完整检查通过。
