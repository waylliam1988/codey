# 明确JSON字符串契约：三格流式定向复验

日期2026-11-18；状态`INCOMPLETE / LABEL_INTERFACE_FAILURE`，冻结提交`c69c85d7`。
[v2](portable-context-labels-v2-20261118.md)因首格返回Python字典字符串而按规则停止，
该失败和其余5个未启动格保留，不重试或补齐v2。

## 冻结变更

本候选使用`--protocol labels-v3`。保留v2枚举、严格JSON解析和全部评分条件，
只在任务末尾明确done.summary是包含JSON的字符串，要求双引号、禁止Python字典，
共同给出unknown/neutral格式示例。示例不包含开启臂预期的历史选择/理由/目标值。
原有`legacy`和`labels-v2`任务均保留，不替换它们的提示或修改历史分数。

模型、endpoint、temperature、native工具和上下文配置与v2一致；output8192、stream=True，
admission关闭/开启/当前指令覆盖三臂，3次正常生成，各用独立state和同样合成经历。
缩小为流式配置，优先验证本轮尚未实测的用量链路；不声称补齐v2或非流式新接口验收。

主观测：三次流式物理请求都包含include_usage；保留usage解析前的JSON事件及应用记录，
输入/输出tokens应三次都为服务报告非负整数。缺失留null，不计通过。
另报告format、choice、reason、联合召回、priority、tone标签和关闭臂unknown。
格式有效但内容选错继续记分，不转为成功；缺失用量继续记为缺口。
服务错误、非法JSON/标签或不完整回复首错停止，记录INCOMPLETE，不自动重试。
每格一次调用，Ghost额外生成0，不用HF或第二模型，不解析Python字典、不替换引号，
不使用语义宽松评分、事后改分或扩样本。

该协议测标签接口与遥测，不能证明自然语气、开放偏好学习或统计收益，也不能将格式改善
归功于记忆机制。来源开关和当前要求覆盖的边界、原始证据留存规则与v2相同。

```powershell
python -m tests.manual.ghost_memory_routing.portable_context_trial --protocol labels-v3 --output .e2e-artifacts/portable-context-labels-v3-20261118
```

## 结果

2次物理请求、2份完整回复，1格成功评分；第2格非法JSON后按规则停止，覆盖臂未启动。
没有服务故障、自动重试、额外Ghost生成或事后更改scorer。

| 流式臂 | JSON/标签格式 | 行为字段 | include_usage | 实际usage |
|---|---|---|---|---|
| off | 有效 | unknown/neutral基线正确 | 已发送 | 缺失，tokens为null |
| on | 无效：summary为单引号字典字符串 | 未评分 | 已发送 | 缺失，tokens为null |
| override | 未启动 | 未评分 | 未发送 | 无 |

两格各保存5个JSON事件，均没有usage字段。原话/目标/偏好在off请求全部不出现、on请求全部出现；
两次都是一个完整done，没有重发。输入/输出用量均未知，不拿估算值冒充服务报告。
off完成耗时4375ms；on因格式失败未保存完整格指标，不补造耗时。

脚本漏接include_usage已修，但本服务配置下流式用量仍然缺失；标签接口也未完成验收。
不能把“枚举写明、要求双引号”宣称成可靠结构化输出，更不能据此宣布偏好学习通过。
[脱敏指标](../../tests/manual/ghost_memory_routing/reports/portable-context-labels-v3-20261118.json)。
原始请求、JSON事件、回复、usage、state、failure、audit及模型前/失败后快照均留
`.e2e-artifacts/portable-context-labels-v3-20261118`。本轮v2/v3合计3次生成，不与历史12格拼接。

## 服务侧定位及限制

只读`/api/extra/version`报告KoboldCpp **1.117.1**。核对该版本
[上游源码](https://github.com/LostRuins/koboldcpp/blob/v1.117.1/koboldcpp.py)：

- [普通流式](https://github.com/LostRuins/koboldcpp/blob/v1.117.1/koboldcpp.py#L5828)
  在include_usage为true时发送usage-only终末事件。
- [OpenAI工具调用的同步转流式分支](https://github.com/LostRuins/koboldcpp/blob/v1.117.1/koboldcpp.py#L7324)
  在[结束位置](https://github.com/LostRuins/koboldcpp/blob/v1.117.1/koboldcpp.py#L7443)
  直接发送最终choices和DONE，没有相同usage逻辑；本次事件形状与该分支一致。
  脚本配置修复之后仍无usage，已有明确的服务分支定位，不归因于Ghost记忆或回答文本。
- [工具参数类型转换](https://github.com/LostRuins/koboldcpp/blob/v1.117.1/koboldcpp.py#L3426)
  对要求string的非字符串值使用`str(val)`，对象会变成Python字典文本。这是与单引号现象
  相符的服务侧路径；本次没有模型原始token或服务内部trace，不能确认该次是模型直接生成
  字符串还是服务转换对象，更不能全归因于模型不遵守指令。

上游文件只读下载留本地，SHA256为
`d29a56f2ec17bba3442fd1ac53a328b95acca43e8895370df0202e87f2a8c5e1`。
版本号相同不等于已核验本地二进制与上游完全一致；没有修改本机KoboldCpp或生产适配器。
客户端HTTP/SSE回归已证明usage-only终末事件存在时能被观测、解析并送达两个observer。
下一步应修服务的工具流遥测及验证可约束的结构化接口，不继续仅换提示词补跑。
Codey的正常done.summary允许自然语言；不为实验把生产摘要改成JSON专用接口。

## 最终检查和提交

初始相关回归53项通过，最终脚本/HTTP/SSE专项13项通过（14.03秒），未运行全量pytest。
最终专项命令：`python -m pytest tests/test_ghost_portable_context_trial.py -q --maxfail=2`。
旧短语仍判未命中、Python字典仍拒绝；真实HTTP/SSE验证终末usage、转发、缺失不补零、
无重试，以及两版正常入口来源消费。

全仓`python -m ruff check .`、完整生产树的
`python -m mypy codey --python-version 3.11`（706文件）、
`pyrefly check codey --python-version 3.11`、`ty check codey --python-version 3.11`及
`python -m tools.kernel_type_precision_gate`全部通过。Pyrefly仍有35项既有suppression及160条warning。
两个修改的Python文件另经mypy/Pyrefly/ty专项检查通过；五项额外disallow-any扫描本轮0诊断，
但未修改依赖331文件/11849诊断导致命令退出1，不称额外扫描整体通过，不新增豁免。
完整检查输出留`.e2e-artifacts/portable-labels-v3-*`。

交付前工作区出现同事新的collaboration生产接线，另外运行一次完整检查，不能将旧快照的
通过冒充当前工作区全部通过：13项专项再次通过（14.22秒）；生产mypy709文件、
Pyrefly、ty及kernel精度门通过；**全仓Ruff退出1，6条诊断**，位于同事在写的
`collaboration.py`（I001、SIM102）、`collaboration_context.py`、`ghost_post_turn.py`、
`personal_context.py`和`toolchain/definition.py`（后四者I001）。这些变动不属于本批，
没有改写、暂存或纳入本提交。完整输出及检查前后Python文件哈希留
`.e2e-artifacts/portable-labels-delivery-*`；该次检查期间哈希无变化，后续并行修改另行验收。
本批不涉及前端；并行前端发布和运行配置变更也不纳入本批。

修复与v2冻结`39b7adf4`；v2失败记录`9b0a1fb8`；v3冻结`c69c85d7`。
本结果随独立实验记录提交，不作生产晋级，不改变既有默认消费范围。

后续另行冻结[直接原生字段接口冒烟](portable-context-native-fields-20261118.md)，六格完成。
该候选移除summary内嵌JSON，使用独立评估工具；不重判本页v3失败或INCOMPLETE，
不改变本页缺失用量及评分，也不作为生产done解析的修复。
