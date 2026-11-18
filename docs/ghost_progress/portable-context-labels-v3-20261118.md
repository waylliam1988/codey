# 明确JSON字符串契约：三格流式定向复验

日期2026-11-18；状态`FROZEN / NOT_RUN`，实机前独立提交协议与脚本。
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

待按上述独立冻结协议运行。
