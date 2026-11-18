# 跨会话项目经历消费者：冻结协议

日期2026-11-18。目标行为：新会话回答“上次选了什么、为什么”时，使用同项目已完成
经历中的选择与原因，保留“尚未验证”的状态，不从记忆获得工具权限。

## 假设、变量与消费者

真实 writer 已按 project scope 检索；`permissions` 未登记经历且 adapter 的 admission
来源显式为空。候选只将已有有界经历经独立 source 接入 `project_adapter.run` 的正常
主调用。双角色预算修复固定用于两臂，不把它和开关差异混合评分。
先在 manual harness 内替换 profile/source builder 做实验，不向生产 import harness。

on/off 两臂具有相同当前问题、任务授权、预算、位置和单次调用出口；on 投递同项目
相关经历，off 不投递。没有新增学习机制，也不声称预测/World Model 或反馈闭环完成。
这是经历连续性相对无历史的首个消费者对照，后续用户理解研究须另与相同经历检索比较。

## 冻结配置与数据

- 单目标 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，
  `http://127.0.0.1:5001/v1`，OpenAI Completions/native tools，temperature0。
- context/reserve/keep_recent=262144/32768/32000，output128，timeout120s，
  max_turns1、physical attempts1；无工具授权（仅control/done），无第二遍/补发。
- 20条独立合成项目经历流；每流两个新 provider/会话，交替on/off顺序，共40请求。
  每流有不同project、source/run身份；选择在borosql/lumenlog间均衡，五种原因轮换。
  每条有3000字符用户请求、助手首部的选择/原因及“validation remains pending”。
- 当前问题逐字相同：要求回复 `<choice> | <recorded reason>`；没有依据回复 `UNKNOWN`。
  不让未来事件进入过去输入。没有开发集选参；20条均为冻结留出矩阵，不根据结果补样本。
  合成流不是20个真实用户；不报告真实用户总体统计或跨模型收益。

## Scorer、收益与成本门

只strip外部空白；最终用户可见summary必须逐字等于fixture中的`choice | reason`。
不剥围栏、不改写回复、不用第二模型评分。off碰巧猜对也按同一规则计正确。
on≥18/20且相对off增加≥8/20；两种选择各至少9/10，任一不达门即无晋级。
on/off完整配对，每格只发一次；完整矩阵未过门保留负结果，不换主对照或scorer。

附加输入tokens每请求≤600（服务报告）；on p95总延迟≤off p95×2+1秒。
同时报告输出tokens、物理请求数、原始请求字节、可见回答时间与总延迟。
当前非流式native传输没有逐token首字时间，测最终用户可见summary出现时间；不把它
冒充网络首token。fixture准备、状态/检索时间独立记录，不隐藏为模型收益。

缺模型ID、缺配对/usage、服务异常、空/截断回复为INCOMPLETE并停止；保留失败证据，
不记模型阴性。不重试，不启动/重启模型服务。格式无效但完整的回复按scorer失败。
健康检查只读，已确认模型ID匹配，不计入40次生成。

## 晋级与边界

通过后只对上述模型/config的普通coding_writer启用；未测模型回退空经历。
正常App/InputQueue→保存→冷开→新会话writer→独立source/admission→HTTP必须通过。
补禁用/未测配置/跨project、删除/重建/重开、组装后关闭与删除、故障隔离。
原22格跨会话失败断言保留，不用manual收益替代生产入口；复用相关receipt/window/TTL/
删除/恢复回归。用户关闭retrieve/admit可停止投递；撤销新增profile/source接线可回滚，
不迁移或删除经历。完整静态门与owned源文件Any审计通过后才提交生产启用。

用户理解、反馈归因和新增信息World Model消费者仍是下一批，不能由本实验代称完成。
