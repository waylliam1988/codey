# World Model：验证结果的预测复盘消费者

- 日期：2026-11-18
- 状态：`ABANDONED`（本候选接口的完整实机负结果；World Model 整体未完成）
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

## 原型与可复现入口

实验实现位于
[world_model_verification_review.py](../../tests/manual/ghost_memory_routing/world_model_verification_review.py)，
生产树不 import 本脚本。它在隔离目录实际执行已知的成功、缺依赖、断言失败 Python check，
由现有 completion owner 生成 proof 后先提交本地 verification fixture，再由预测器只读消费。
这些是实验 fixture，不是用户项目的实际运行记录，也不声称环境故障诊断本身是新能力。
预测采用持久性假设：下一次同命令/同 workspace 类别与上次一致，confidence 固定 0.6，
不视为校准后的概率或 truth confidence。

review 只接受更晚且已发生、同命令/同 workspace/同 scope 的有效 verification，原始 proof
ref 和 bounded digest 保留；没有有效来源则 unjudged。projection 在来源移除、关闭检索、
scope 不符、过期、未复盘时为空；这些是纯投影源存活检查，不能冒充完整生产持久删除/
reset/故障恢复验收。保留实验脚本供复查，未证明收益前不新增生产存储或后台服务。

独立 ContextSource 的 key 为 world_model_verification_review，900 字符，拒绝截断；
PromptEnvelope 后对同一待发送文本调用现有 admission，记录 digest/source ref/epoch。
关闭准入或过期会在调用 provider 前拒绝。live 使用新的 ApiProvider 对象和单次尝试，
不复用对话历史，不执行工具，保存完整 prompt/reply、reasoning、usage 和错误于本地。
未完整响应或服务异常停止本批，不自动重试或补样本。

```powershell
python -m pytest tests/test_world_model_verification_review.py -q --tb=short
python -m tests.manual.ghost_memory_routing.world_model_verification_review --output .e2e-artifacts/world-model-verification-review-20261118/offline
python -m tests.manual.ghost_memory_routing.world_model_verification_review --output .e2e-artifacts/world-model-verification-review-20261118/live --live
```

live 输出目录已有 metrics/failure 时拒绝覆盖；复现须使用新的隔离目录，不删除旧证据。
前置健康检查目标模型 ID 一致，未把健康检查计为 inference 样本。

## 实际结果与判定

冻结提交 `bd972fd9` 后实现原型，40 次实际模型请求全部完整返回，没有服务失败、截断、
工具调用、空回复或补样本；前后 `/v1/models` 均为上述 Gemma 模型。on/off 各 20 次，
严格 scorer 均为 **0/20**；40 条回复都带 Markdown JSON 代码围栏，因此是 invalid_json。
这是受测接口的模型格式负结果，不是模型服务阴性；也不能由零有效 action 推导预测内容
本身无效。没有剥围栏重判、修回复、替换 scorer、放宽门槛或把这些样本算作格式正确。

| 指标 | off | on |
|---|---:|---:|
| 完整响应 | 20 | 20 |
| 严格 action 通过 | 0 | 0 |
| invalid_json | 20 | 20 |
| provider 报告 input tokens 总数 | 5840 | 6792 |
| provider 报告 output tokens 总数 | 280 | 280 |
| 平均请求耗时（秒） | 0.64525 | 0.65170 |

额外投递 4716 字符、952 个 reported input tokens（+16.3%），未增加冻结指标收益。
耗时只作本机观察，不据 20 题声称显著延迟变化。三类已复盘任务共 12 个 on 请求有独立
admission；两类 unjudged 的 8 个 on 请求提示为空，与对应 off prompt digest 完全相同。
逐 case、逐题/arm 的脱敏指标及命令见
[报告](../../tests/manual/ghost_memory_routing/reports/world-model-verification-review-20261118.json)。

本候选未通过净增至少 3/20 的冻结晋级门，终止该文本复盘→严格 action 接口候选，
**不接生产、不默认启用**。代码只留在 manual 实验目录供复查；没有在 codey 树新增
无消费者模块、持久化 manager、默认关闭的功能或主任务额外模型调用。
本地完整 prompt/reply/reasoning、proof fixture、usage、stdout/XML 和冻结哈希位于
`.e2e-artifacts/world-model-verification-review-20261118`，仓库无原始回复或完整 state。

## 检查与限制

原型 24 项验证真实 check→proof→prediction→review→投影→admission 消费链及非法输入。
最终相关 11 文件 192 passed / 31 subtests，零失败/错误/跳过。全仓 Ruff 与完整 702 文件
生产树 mypy/Pyrefly/ty/precision gate、两个新文件定向三类型均通过。额外 Any 审计 owned
零诊断，exit 1 来自未修改依赖的 4630 项/193 文件诊断；未新增豁免、cast、ignore 或改范围。
Pyrefly 全树仍有既有 35 suppressed/158 warnings，不声称全仓没有 Any。
live 与最终回归的两个新文件 SHA256 完全相同，702 个生产文件在检查期间冻结不变。
全部命令和哈希见脱敏报告。
不运行全量 pytest，无前端或静态资源变更。

编写阶段先修正 argparse 的 Path 被送入 JSON decoder、datetime 子类返回注解、
Pyrefly None 收窄/空容器及容器上下文产生的 6 项隐式 Any；均发生在 live 源码冻结前，
没有修改原型的动作门槛或对照定义。初期检查输出保留于本地，不能计作生产负结果。

本轮只证明隔离原型与该模型/协议/预算的格式负结果；不证明完整生产生命周期、硬退出、
真实用户运行历史上的收益、可校准概率或跨模型能力。source 存活重算测试也不是持久
delete/reset/混版本恢复证明，因此未标为 PRODUCTION_MERGED。现有生产默认均不变，
无需生产回滚；停止执行此手动脚本即可隔离实验。

下一步审查：如继续此方向，应另行冻结更贴近正常消费者的结构化传输或人工复盘界面，
保持原 scorer 和本次负结果，先解决格式适配再评估语义收益，不能用重跑后最好结果替换
本批。另需选择有新增预测信息的任务；重复当前 completion 事实很可能只增加 token。
完整 G0 的排序/digest、跨 run 恢复与 World Model 真实生产闭环仍待推进。
