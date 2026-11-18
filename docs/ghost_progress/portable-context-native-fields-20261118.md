# 原生字段工具：本地上下文接口定向冒烟

日期2026-11-18；状态`COMPLETE / ISOLATED_NATIVE_INTERFACE_SMOKE`。
实机前独立冻结协议与脚本，提交`cf8e7c2d`；本记录追加执行结果，不修改冻结指标。
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

六格均完成，各一次物理请求、一次有效原生工具调用；无重试，Ghost额外生成0。

| 模式 | 臂 | 格式 | 选择/理由 | 目标/语气标签 | 关闭臂unknown/neutral |
| --- | --- | --- | --- | --- | --- |
| 非流式 | off | 有效 | 无历史，不要求恢复 | 无历史，不要求恢复 | 正确 |
| 非流式 | on | 有效 | opalpanel/offline_cursor_sync | keyboard/warm | 不适用 |
| 非流式 | override | 有效 | opalpanel/offline_cursor_sync | animations/technical | 不适用 |
| 流式 | off | 有效 | 无历史，不要求恢复 | 无历史，不要求恢复 | 正确 |
| 流式 | on | 有效 | opalpanel/offline_cursor_sync | keyboard/warm | 不适用 |
| 流式 | override | 有效 | opalpanel/offline_cursor_sync | animations/technical | 不适用 |

脱敏指标见[报告](../../tests/manual/ghost_memory_routing/reports/portable-context-native-fields-20261118.json)。
原scorer对off的recall/priority/preference均为false，保留原值；该臂验收依据是baseline_unknown=true，
不能将它算成召回失败。独立wire审计核对工具schema无summary、四个字段直接传参、
单请求、off三类来源全无、on/override全有以及实际标签，均与指标一致。

非流式三臂input/output分别437/35、718/40、729/40；流式三臂usage均null，
已发送include_usage但服务没有返回。按冻结约定，此次接口测试不因缺失用量失败，
也不宣称完整成本门通过。六格耗时约1515/11906/9546/6844/7204/8062ms，
只作描述，不推断性能收益；本地模型服务可能有其他使用者。

每格前后源码/配置快照均未变化，最终sources-before/after完全一致，
快照SHA256为`a8e332b8667e96c7e57c6ef967c6828ca0e8dbe9cbdb22437af88307787009de`。
完整请求、原始回复、state、用量、模型及源码快照、audit留本地
`.e2e-artifacts/portable-context-native-fields-20261118/`，不入库。

## 回归与静态检查

最终相关回归74项通过，命令：

```powershell
python -m pytest tests/test_ghost_portable_context_native_trial.py tests/test_ghost_portable_context_trial.py tests/test_ghost_context_admission.py tests/test_ghost_portable_context.py -q --maxfail=2
```

覆盖直接字段及拒绝分支、真实HTTP/SSE三臂、未知用量、不重试、物理发送前关闭/删除/撤回重读、
错误prompt/scope及既有正常消费边界。未运行全量pytest。较早同命令64通过/2失败，
原因是并行修改的research fixture缺run_id；同事修复后最终同命令74通过，未由本批改写该测试。

本批三个Python文件的Ruff、mypy、Pyrefly、ty专项均通过。额外严格Any审计在这三个文件中0诊断，
但未修改依赖332文件/11837诊断导致命令退出1，不声称该额外命令整体通过。
最终全仓Ruff、完整生产树mypy（709文件）、Pyrefly、ty及kernel_type_precision_gate全部通过。
中间一次完整生产树Pyrefly退出1，5个bad-argument-type，均在同事当时未提交的
`codey/operations/kernel_preparation.py:40/42/55/59/61`，guidance容器被推断为LiteralString列表。
同事后续调整该文件后，本批再次执行相同74项回归及五个完整静态门，均通过；
该生产文件未由本批改写或纳入提交。前次失败日志保留，不将其改写成通过。
Pyrefly另有35项既有suppression、160条warning。

初次全树mypy因共享增量缓存内部断言崩溃，改用独立cache目录后同范围检查通过；
最终命令为`python -m mypy codey --python-version 3.11 --cache-dir .e2e-artifacts/mypy-native-v4-handoff`。
其他全树命令按仓库约定执行。完整输出、前后哈希与归属见
`.e2e-artifacts/portable-native-v4-handoff-*`，检查期间源码无变化；中间失败日志见
`.e2e-artifacts/portable-native-v4-delivery-*`。
专项、Any审计及回归日志见`.e2e-artifacts/portable-native-v4-*`。

## 结论与下一步

本配置、单个合成经历下，直接原生字段接口完成了六格来源消费和当前覆盖冒烟，
避开了让模型在自然语言summary中再序列化JSON的接口负担。
v2/v3失败仍保留；因上下文构造也不同，不把此次通过解释成schema的单变量因果证明。
生产done.summary本来允许自然语言，没有据此修改生产解析器或接受Python字典。

下一步结构化测量可复用此评估入口；开放偏好学习、自然回答语气效果和真实协作收益需各自冻结实验。
四个枚举只服务本次评分，不是新增的生产偏好词表。本次未修改生产代码、未默认启用新功能，
不标记PRODUCTION_MERGED，也不代表World Model或自主学习完成。
