# 原生字段工具：本地上下文接口定向冒烟

日期2026-11-18；状态`FROZEN / NOT_RUN`，实机前独立提交协议与脚本。
脚本`tests/manual/ghost_memory_routing/portable_context_native_trial.py`，协议`native-fields-v4`。
[v2](portable-context-labels-v2-20261118.md)和[v3](portable-context-labels-v3-20261118.md)的
JSON字符串失败、未完成状态、未知用量和旧评分均保留；本轮不是补跑或重判它们。

## 目标与接口变更

检验将choice/reason/priority/tone直接声明为原生工具字段后，能否稳定接收严格标签并观察
本地上下文开关及当前指令覆盖。移除done.summary内嵌JSON的第二层序列化，
不靠继续强调引号或解析Python字典纠正回复。

只声明`record_context_probe`一个工具，四个必填string字段带原枚举，additionalProperties=false；
要求一次原生工具调用，函数名、调用ID、字段集合、类型、枚举、完成状态全部本地校验。
错误工具、多个调用、无调用、附加正文、截断、额外字段、短语或非法标签均拒绝；不补发或修复。
成功解码后由Python序列化为原scorer输入，评分条件不变，模型无需生成JSON字符串。
共同任务保留原场景、unknown/neutral规则、枚举标签及warm映射，改为要求直接调用新工具。

## 实验范围与生产边界

合成源经历仍通过正常输入队列/Chat fixture记录，人工确认warm偏好和keyboard项目目标。
消费时复用生产的`validated_project_preferences`、`validated_project_experiences`、
`validated_project_goal_sources`、独立GhostContextSource及`admit_ghost_context`。
物理请求gate重读源快照，确认准入的完整prompt恰好出现一次，再检查scope/预算/控制；
保留provider自动追加的时钟消息。实际wire检查三类内容在off全无、on/override全有。

消费调用是**独立评估工具入口**，直接调用同一ApiProvider，不是生产writer完成路径，
不伪造done、完成proof或工具执行，不把评估调用记录为生产任务完成。
不修改生产done契约、权限或模型适配器。正常writer/Chat接线的既有回归仍作为独立证据；
本轮不能声称重新证明完整正常入口或默认启用了新功能。

## 冻结配置与验收

- 单个合成项目和同样经历；Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M，
  `http://127.0.0.1:5001/v1`、Chat Completions、native tools、temperature0。
- output8192、context262144、reserve32768、keep32000；非流式及流式各off/on/override三臂，
  共6格，固定顺序先非流式再流式，每格新state/新provider/新消费会话。
- 单格只允许一次生成、一次物理请求；种子使用确定性fixture不调用模型，Ghost额外生成0。
  不加载HF，不用embedding、第二模型、多候选或回答修复。
- 全部臂使用相同工具schema；关闭臂应unknown/neutral，开启臂应
  opalpanel/offline_cursor_sync/keyboard/warm，覆盖臂把后两项改为animations/technical。
  描述性报告格式、choice、reason、联合召回、priority、tone标签和关闭臂unknown。
- 边界通过要求6/6单调用、格式有效、可复核来源全有/全无及当前覆盖。
  内容选错但格式合法继续记录，不把错误选择改分；格式/服务/来源边界首次失败立即停止，
  保存INCOMPLETE和已完成格，不自动重试、不扩样本。
- 用户已确认当前KoboldCpp工具流不提供用量。仍按正常Local配置发送include_usage并保留
  原始JSON事件/usage，但缺失为null，**不是格式或行为失败、不是零成本**；不以用量缺失
  阻塞这次接口测试，不宣称完整成本门通过。耗时为描述性总耗时，不统计性能收益。
- 保存生产Python、runtime配置及实验实现的SHA256；每格前后核对，变化则停止，
  防止同事并行改动混入同一冻结矩阵。模型前后快照留本地。

这是一个合成样本的接口冒烟。成功只支持本配置下原生字段消费/来源与覆盖边界，不能证明
开放偏好学习、自然温暖度、算法优越性、全模型收益或World Model晋级。
上下文构造与旧kernel提示不同；不能用准确率差异推断单独的schema因果收益。

## 复现与证据

```powershell
python -m pytest tests/test_ghost_portable_context_native_trial.py tests/test_ghost_portable_context_trial.py -q
python -m tests.manual.ghost_memory_routing.portable_context_native_trial --output .e2e-artifacts/portable-context-native-fields-20261118
```

请求、JSON事件、原始原生参数、admission、usage、state、attempt及源码哈希留本地；
仓库只保留可复现脚本、协议和脱敏指标。成功接口不是新生产消费者，无生产启用或回滚开关；
回滚本轮实验提交即可，已有本地记忆和生产消费不受影响。

## 结果

待按本冻结协议执行。
