# 条件化讨论开头与用户反应预测：冻结负结果

日期2026-11-18。状态`EXPERIMENT_COMPLETE`，生产决策`EXPERIMENT_ONLY`；
**按冻结门终止本候选，不接生产、不补样本。**
冻结实现/协议提交`7fe3f37a`；[运行前协议](interaction-feedback-protocol-20261118.md)，
[脱敏逐格报告](../../tests/manual/ghost_memory_routing/reports/interaction-feedback-20261118.json)。
此前项目经历连续性生产提交`35fb56da`保持启用及其原模型/config边界。

## 本轮增加了什么可复查闭环

用户可见目标是同一方案问题根据各自历史先比较或先推荐，明确纠正后改变，临时要求到期回退。
20条合成个人经历流、10组相同当前方案题与相反个人历史，三臂均使用同一时点的语义历史。
无历史、仅检索相同经历、经历加独立预测，共5阶段、**300次物理生成**。
这是shared kernel/durable/managed outputs/native done/physical gate上的真实模型回复实验，
不是仅检查提示已发送；不是普通App生产接线，也不是20个真实用户或开放对话总体测量。

保存的明确依据→当前有条件理解→实际回复→绑定ref/scope/context/完整digest的明确反馈
→review→修正或维持→冷读重放→下轮有界来源，都在实验中实际执行。
新World Model对象是后续用户反馈要求哪种讨论开头，有情境、来源、horizon和判定条件，
不是重复当前项目验证事实。预测仅为独立advisory ContextSource，不产生事实、Evidence、
权限或完成裁决；采用简单“最近合法明确反馈”规则，不主张新增学习算法收益。

反馈在三臂完成之后才写入下一阶段共享历史；未来反馈不进入过去输入。
这是候选实际交互轨迹上的反事实比较，不冒充三组独立在线用户。
人工明确偏好/合成明确反馈是受控信号，不是自动把LLM理解写成已确认偏好。

## 完整结果与决定

主指标是每条流四个主要阶段全部正确；不能用较高的逐格总分替代。

| 臂 | 四个主要阶段全对的流 | 全部阶段逐格正确 | 格式有效 |
|---|---:|---:|---:|
| 无个人历史 | 0/20 | 56/100 | 100/100 |
| 相同经历检索 | 0/20 | 65/100 | 100/100 |
| 经历加预测 | 0/20 | 70/100 | 100/100 |

预测臂按阶段正确数为10/20、10/20、20/20、10/20、20/20：初始理解、永久反转与临时
到期后的行为均未达到18/20。初始两类的主要流全对数均0/10。
阶段4两独立边界各10/10（换情境、当前明确要求）；它们不计作个人适应收益。
预测比检索多5个单格不满足预定主指标、增量门及分组门，**收益门未过**。
所有300回复格式有效，不能把此次失败改写为JSON/原生工具通道故障；也不能从格式通过
推断语义适应成功。此结果只否定受测模型/config与该候选接口的晋级，不推论所有World Model、
Hebbian或个人适应机制整体无效。

100个review为40 hit、40 miss、20 unjudged。前两次明确改变要求使相应未来偏好预测miss；
这不等于40条用户不满意或40个先前任务错误。阶段4没有可归因反馈，保持unjudged。
实际行为准确率、用户主动改变要求与预测准确性分开报告。

目标Gemma4-12B QAT、loopback Completions/native、temperature0、非流式、output128，
窗口/安全/近期预算262144/32640/32000，均按协议。无缺失usage、服务故障、重试、
追加样本或embedding；开始/结束models匹配。没有第二模型评分或重新解释回复。

## 成本与测量边界

成本门通过：相对相同经历检索，额外输入最多98tokens（门400）；
p95总延迟预测2625ms、检索2578ms、无历史2297ms；每个任务一次物理生成。
输出tokens、wire bytes、准备耗时、逐格总时间及可见时间保留在报告。
非流式可见时间是kernel返回，不是网络首token，也没有流式验收结论。

每流新增理解JSONL最大2691bytes，按冻结代码测该文件；共同任务SQLite及本地审核产物
不属于该字段。完整本地live证据共95652371bytes，另行记录，未隐藏为“总state仅2691bytes”。
preparation_ms覆盖cell组装与durable初始化，外层JSONL读取/predict未单独计时；
不将本次p95宣称为完整产品端到端时延。无收益，因此不追加成本或质量实机补测。

## 验证、提交与后续

冻结前45项契约测试通过，包括时间因果顺序、永久/临时状态、独立来源admission、
删除/关闭的确定性投影拒绝、错误身份/时间/隐式反馈unjudged、严格解码及冷重放，
并有真实kernel→HTTP缺usage只发一次的测试。它们不替代普通App全生命周期生产门。
初次夹具误以为require_all拒绝返回对象、漏建durable operation，均在冻结前按真实契约修正，
未更改生产；最初额外Any中的max key/loader回调也在冻结前明确签名。

最终6文件**98 passed / 2 subtests passed**，含新增45项、既有continuity trial、ContextSource、
admission、Attempt Gate及kernel domain契约；原生产703文件与`35fb56da`字节一致。
最终指定五门按顺序全部通过：

```powershell
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
```

累计16 owned Python文件（前批14加本批2）常规三检查器及额外
`--disallow-any-expr --disallow-any-explicit`的owned错误均0，无新cast/ignore/豁免。
专项mypy完整命令仍返回1：未修改依赖8文件269项常规、351文件14107项额外诊断，
完整输出与路径分列保留；不称专项命令全部零退出或全仓Any清零。
Pyrefly生产树35 suppressed/159个未显示警告、owned11个未显示警告分列。
最终711源文件hash、冻结两脚本hash及703生产文件不变均已核对；`git diff --check`通过。
未运行全量pytest、未改前端、未创建/复制worktree。

原始回复、JSONL、SQLite、wire、models与日志仅留本地
`.e2e-artifacts/interaction-feedback-20261118-live/`；验证和hash在
`.e2e-artifacts/interaction-feedback-20261118-contract/`。
仓库只保存冻结脚本/测试、协议、脱敏指标与本负结果，无新增生产模块或默认关闭空壳。

本候选终止；完整的个人适应生产闭环和有净收益的World Model生产消费者仍未完成。
下一候选须说明新的真实消费者、变量及原证据缺口后另行冻结，不只换提示重复本候选，
不重跑两个项目验证事实负结果或Hopfield矩阵。既有中文及项目连续性生产功能保持各自状态。
