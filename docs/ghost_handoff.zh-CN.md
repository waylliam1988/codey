# Ghost / World Model 团队交接说明

> 交接日期：2026-10-21
> 基线提交：`3731c954`（Ghost 文档、路线图、实验脚本与历史快照归档）
> 适用范围：Ghost、World Model，以及它们进入 Codey runtime 的实验和生产准入工作

这份文档可以直接转发给接班同事。它是工作方法和当前判断的交接记录；路线图中的版本号只是内部验收门槛，不要求连续实验按版本号命名。具体验收条件仍以 [Roadmap](../ROADMAP.zh-CN.md) 为准，实验细节以 [Ghost 方向与晋级协议](ghost_future_direction.zh-CN.md) 和 [memory-routing 实验目录](../tests/manual/ghost_memory_routing/README.md) 为准。

> 2026-11-17 晋级要求修订：以一个实际使用的本地模型和配置为验收单位；行为收益与相关生产边界通过后，可在受测范围正式启用并按合并事实标记 `PRODUCTION_MERGED`。第二模型与跨模型验证为可选扩展，不阻塞单模型生产晋级；未测试模型回退基线。历史测量、样本门槛、对照及生产边界要求保留，本次规则修订不自动改变任何实验的完成或合并状态。

> 当前交接任务明确包含推进 World Model 的真实闭环。本文历史上的 optional/shadow 表述
> 描述未验证阶段，不意味着本轮可以只交提案；前置边界通过后继续真实消费者和效果评估。
> 单模型有收益且相关生产边界通过，即在受测范围接线、默认启用并更新状态；没有收益保留负结果。

2026-11-17 最新完成：[原生回执准入拒绝与恢复组合验收](ghost_progress/native-refusal-recovery-20261117.md)。
当前共享kernel、真实SQLite/HTTP、两协议和三scope的36项通过，相关12文件187项通过；
成功/错误工具回执在两次NOT_SENT拒绝后可暖/冷恢复，原窗口与call id保留，工具不重复执行。
Thread回调类型修订后36项复验、全静态门及新增测试owned Any零诊断；两批不相加。
现有唯一Attempt Gate与续传已默认生效，本轮只补证据，不重复生产实现或新加默认关闭开关。
首轮36个审计名夹具失败保留；仍缺完整App/硬退出/跨run、其他发送维护边界和World Model。
继续按最新进度推进，不重跑已停止的语言批次。

此前完成：[continuity过期持久事实与消费者](ghost_progress/continuity-expiry-production-20261117.md)。
普通维护追加有界过期事件，旧同步日志按已记录维护时间重建；reader/builder/真实队列
筛选过期项并共用逻辑时间，无新增默认关闭开关。同来源可重新出现，原始历史保留。
原18项3→18、旧日志9项3→9通过；75项新增，最终44文件1064/4子测试及全静态门通过，
五受检文件额外Any零诊断。原始负结果`92276fec`及候选/夹具/类型失败保留。
继续其余维护/排序/digest/replay和真实发送/controls验收，再推进World Model；不重跑已停止语言批次。

此前完成：[Sleep显式维护时间](ghost_progress/sleep-maintenance-time-production-20261117.md)。
普通Sleep默认一次捕获maintenance_as_of并传至衰减、continuity刷新、租约回收和Affinity
来源过期筛选，报告保留逻辑时间，独立维护入口同样支持显式时间。原15项0→15通过；
63项新增，扩大1047/36子测试、最后计数断言/冗余转换修订后281/2子测试及完整静态门通过；
九受检文件额外Any零诊断。无新增默认关闭开关或模型调用；原负结果`fff79f94`保留。
continuity过期持久投影/重建随后闭合见上；继续其他维护入口、稳定排序/digest与其余领域replay，再推进真实World Model。
不能据此宣布完整0.6.4、0.6.1–0.6.3或World Model完成，不重跑已停止语言实机批次。

此前完成：[重放历史时间校验](ghost_progress/replay-timestamp-validation-production-20261117.md)。
原12项3→12通过，九项坏时间默认拒绝并原子保留原日志/事务前有效投影；71项新验收、
候选211、最终523/36子测试及全静态门通过，六受检文件额外Any零诊断。首轮扩大
1194/1为旧测试在临时目录销毁后读DB，已修正并清理全文件类型，原断言与失败记录保留。
无新增默认关闭开关；继续显式maintenance_as_of、稳定排序/digest及真实World Model。
本次不等于完整0.6.4或既有坏投影自动恢复。

此前实验：[固定日志重建的隐含时间](ghost_progress/replay-clock-boundary-baseline-20261117.md)。
真实Affinity store固定日志跨两个时钟重建，12 cell全有效：合法时间3通过，损坏强化ts/
衰减ts/快照基点9项均改变实际权重或状态；前两轮夹具scope/对照断言错误保留并排除。
原始实验未改生产；上述后续修复已闭合本矩阵，再推进显式维护时间和World Model。

2026-11-17 最新完成：[observation 兼容消费视图](ghost_progress/observation-consumption-view-production-20261117.md)。
schema9正常owner构造原地保留历史，旧名改为有界原生只读视图；原reader15项13→15通过，
旧writer42项继续通过，另19项真实7/8迁移、回滚、向量外键、序号、缓存SELECT、损坏和
空写入验收通过。候选273、扩大1122/136子测试及完整静态门通过，16个owned文件额外
Any零诊断。首轮266/1的空视图触发器失败保留，使用原生只读修复，不放宽原断言。
无新默认关闭开关；不撤销已有读快照/旧进程内存、不证明整个旧App和实际模型发送。
继续其余生命周期/retention与admission/controls边界、显式时间/replay及真实World Model。
完整0.6.3与World Model仍未完成，不重跑已停止语言批次。

此前实验：[旧 observation reader 容量窗口基线](ghost_progress/legacy-observation-reader-baseline-20261117.md)。
真实 schema7 reader 持有原 store 跨当前升级；15 cell 全有效，13通过、2个capacity的
project/session公开检索及渲染返回窗口外旧词。当前reader、delete/reset、旧版裁剪与
新词可用性对照正常，相关236项回归通过。原始负结果commit为`c956c5ef`，以上后续视图
已限定闭合本矩阵；不能以来源永久退役混淆容量过期与用户删除，不外推实际模型发送。
此前原事件DELETE约束继续有效；完整混版本runtime、0.6.3和World Model仍未完成。

此前完成：[原事件物理删除防护](ghost_progress/history-delete-guards-production-20261117.md)。
真实warm schema7旧写口原42项14→42通过，另13项回滚/缓存SQL/跨升级读快照/既有schema8补装
验收通过；扩大1088项/136子测试、测试行工厂类型修正后13项复验及完整静态门通过，四个owned
文件额外Any审计零诊断。普通owner构造默认在三原始事件表安装DELETE约束，无新关闭开关。
原负结果commit为`7c9ad01f96a1693697cc68c0c6a7a9d3e99c186b`，原14/28结果保留。
下一步核对旧reader/其他混版本事务、磁盘retention/purge与其余生命周期，补齐admission/
controls边界并推进显式时间/replay及真实World Model；本次不等于完整0.6.3或旧App隔离。

此前完成：[普通reset历史保留](ghost_progress/reset-history-retention-production-20261117.md)。
原63项由21→63通过，另50项恢复/容量/回滚/新输入/开关/向量交错通过；扩大906项/136子测试，
import格式修正后125项复验和完整静态门通过，六个owned文件额外Any审计零诊断。
七领域普通reset默认保留原事件、推进空重放窗口并保留永久身份退役，清除派生向量。
下一步验证混版本旧进程、磁盘retention/purge及其余生命周期，再推进显式时间/replay和
真实World Model。基线负结果commit为`b64ab6ef16f0bb0c881c3fd2a24c3c17e99856b4`，保留原21/42；
以下关于reset尚未修复的描述为历史阶段，不能据此重做本次已闭合矩阵或宣布完整0.6.3。

此前完成：[容量检查点与历史保留](ghost_progress/capacity-history-retention-production-20261117.md)
原78项有效矩阵48→78通过，另55项窗口验收；扩大776项回归/136子测试、最后SQL窗口复查后
366项复验及全树静态门通过，两批不相加、不把较早批次当成最终源码全范围验收。
schema8保留原领域事件，以独立确认的有界窗口重放/检索；Sleep同时满足count/bytes且不
复制旧report，observation保留事实并清过期向量。普通消费者默认使用，无待开启开关，13个
owned文件额外Any审计零诊断。范围为正常单版本runtime/冷启动升级；下一步验证reset与其他
历史重写、旧进程跨版本并发、磁盘retention/purge及完整生命周期，随后显式时间/replay与
真实World Model。原负结果、候选空checkpoint及Sleep bytes失败均保留，不能宣布完整0.6.3。

此前：[普通删除历史保留](ghost_progress/scope-history-retention-production-20261117.md)
原63项矩阵39→63通过，inbox/Hebbian/Sleep追加删除事件、observation保留事实并退役过滤；
派生向量同事务清理与embedding期间删除复查默认生效。86项验收、独立ID修订前853项扩大
回归/141子测试、最终源码454项复验/133子测试及完整静态门通过，九个owned文件额外Any
审计零诊断。当前下一步是容量compaction/retention、reset和其他历史重写路径，不能把
低容量普通scope删除通过当作完整0.6.3；随后继续显式时间/replay及真实World Model闭环。

此前：[升级旧投影消费者准入](ghost_progress/legacy-projection-admission-production-20261117.md)
已在普通经历检索、continuity和work queue默认reader/claim接线，原24项矩阵8→24通过。
None/false不补true，原始审计数据保留，独立研究及人工偏好仍可用。47项新增验收、553项相关
回归及8个subtest、最终50项复验和完整静态门通过；五个owned文件额外Any审计零诊断。
扩大回归中的旧A/B夹具失败已在旧源码复现并通过真实输入admission修正，未改scorer。
普通删除低容量历史保留随后已修复，下一步补容量retention、其余升级/异常身份scope、显式时间/replay，
随后继续真实World Model闭环；不重跑已停止语言批次，不将本次修复视为完整0.6.3通过。

此前：[删除操作发现与显式恢复](ghost_progress/deletion-manifest-discovery-production-20261117.md)
已在普通 summary/export/action 默认接线，CLI export 可打印第一页，认证 action可继续分页。
12个响应前硬退出案例、329项相关回归、类型收窄后50项复验及完整静态门通过；四个owned
文件额外Any审计零诊断。原ID恢复仍走现有API，completed不重做、blocked
不自动重试。当前剩余 retention、升级输入、异常身份/scope及完整生命周期，随后推进显式
时间/replay和真实World Model消费者；下文“pending发现待补”属于历史阶段。

此前：[Manifest 默认调度](ghost_progress/deletion-manifest-production-wiring-20261117.md)
已在普通 API/CLI 生效，无待开启的候选开关；同 ID 的 API 重试仅处理 pending，blocked
保留，需要新操作显式恢复。相关回归 741 passed / 141 subtests，类型收窄后 123 项复验
及全树静态门通过。后文关于“Manifest 待接线”描述历史阶段，下一步补 retention、响应前
崩溃的 pending 发现、升级输入与完整生命周期，再继续显式时间/replay 和真实 World Model 消费者。

2026-11-17 最新混合删除修复：[graph 精确替换](ghost_progress/graph-exact-replacement-20261117.md)
已在正常 Hebbian/Affinity owner 默认生效，同一不同操作矩阵由 28/36 提升为 36/36，
相关回归 675 passed / 141 subtests 及全树静态门通过；Manifest 默认控制面调度随后已接线见上文，
不要重跑已停止的语言批次或把本次 graph 修复等同完整 0.6.3 / World Model。

2026-11-17 最新接收生命周期进展：[输入接收登记](ghost_progress/received-input-admission-wiring-20261117.md)
已在新 task commit 前默认接线，覆盖 busy queue、普通提交、headless、重启与限定崩溃边界。
后续 [canonical 接收结果](ghost_progress/canonical-input-receipt-20261117.md)把登记/隔离失败结果
随原输入提交，实际写口与新消费者默认检查；31-cell 双故障矩阵旧输入 12→0，包含恢复及重启。
完整 0.6.1–0.6.4、升级输入、完整 manifest 与混合恢复、身份/scope 异常和 World Model 仍未完成。后文是推进顺序，
不要据此重做已经完成的 0.6.0 或语言实机批次；实际下一步以进度板和最新实验记录为准。

后续 [Manifest 原子 mutation/ack 候选](ghost_progress/deletion-manifest-atomic-mutation-20261117.md)
通过 68 项新增验收、652 项相关回归和另批 48 项恢复/启动复验，包含同操作两进程与三窗口硬退出。
旧 coordinator 的重复删除负结果另存 `e9fb6500`；当时尚未接默认调度。后续不同 operation
混合矩阵已由 graph 修复闭合，正常入口接线见上文，不重复旧探针时序。

## 先读这段：不要把路线图当成无条件功能清单

你接手的是一条**按证据推进、可以暂停或终止**的实验路线，不是把 Ghost 和 World Model 的所有设想逐项实现完。

连续实验按“实验批次、候选功能或研究轨道”推进，不强行拆成版本号。每个实验批次只有在实现、行为测试、deterministic replay fixture、必要的 live/offline 实验、指标报告、回滚说明和文档都完成后，才能标记为 `EXPERIMENT_COMPLETE`。代码写出来但没有证明行为、没有真实发送链路证据，状态仍然是 `INCOMPLETE` 或 `SHADOW`。

如果实验显示没有净收益，保留现有安全边界并记录阴性结果就是实验完成；无效实现不能合并进主代码。只有对生产有明确帮助、通过生产路径和安全回归的代码或功能，才允许进入主分支。

## 当前交接结论

| 项目 | 当前状态 | 交接时必须记住的边界 |
|---|---|---|
| Ghost 基础设施 | 已有可审计、可删除、可重建的本地经验存储和投影 | 这证明基础设施存在，不证明记忆一定改变行为 |
| 历史 memory-routing 实验 | 已归档并提交，含无回复测量、报告、脚本和来源摘要 | 原始回复/state 在被 Git 忽略的 `artifacts/ghost_memory_routing/`；生产代码不 import harness |
| language family | modelA 普通 Chat 的中文子集已于 2026-11-17 接入生产并默认启用；历史冻结评估为 19/20 对 0/20，后续 live 为 20/20 对 0/20 | 仅对[受测模型与配置](ghost_progress/language-production-enablement-20261117.md)启用；当前请求优先，其他模型回退基线；新增实机已按用户要求停止 |
| command / verification habit | `SHADOW` | directive 不是已证明的宿主；要区分命令建议、工具选择、审批结果和任务完成 |
| format / length / answer-first | `WEAK-HINT-ONLY` | 没有稳定的精确服从收益；列表本来就容易通过，不能算记忆收益 |
| self-description | 默认排除 | 空余名额不能回填系统自述 |
| Hebbian / Affinity | 辅助特征、关联和解释来源 | 不拥有最终投递决定权；复杂排序必须胜过简单基线才有资格扩大作用 |
| World Model | 可选 shadow 方向 | 不阻塞 language 闭环；不产生 `evidence_refs`，不裁定事实，不进入默认 prompt |

已归档实验的关键数据是：language on/off 为 `19/20` 对 `0/20`；command 主 on/off 都是 `0/20`；生产 Hebbian cap12 的 language 为 `6/10`，分层 cap12 为 `9/10`，新近 cap12 为 `2/10`。这些数字只支持受测模型和配置下的条件结论，不能把它们改写成“Ghost 已经普遍有效”。

## 推荐接手顺序

默认从路线图中第一个未完成的验收门开始，不能直接跳到 World Model 或大规模学习机制。接班记录使用实验名称，不要求把每次连续实验写成新的版本号。

1. **先确认工作区和基线。** 阅读本文、[Roadmap](../ROADMAP.zh-CN.md)、[Ghost 方向](ghost_future_direction.zh-CN.md)，运行现有的无模型复查；确认当前分支、Python 环境、测试状态和 `git status`。
2. **先做 0.6.0。** 冻结一个目标本地模型及配置、任务、on/off 对照、最低样本量、scorer、收益门槛和缺失数据规则。目标模型缺失、缺 off 或缺样本必须输出 `INCOMPLETE`，不能按 0% 计算；其他模型未测不阻塞该模型验收。
3. **再做 0.6.1。** 把真实 provider-send 路径上的 `ContextSource → PromptEnvelope → Context Epoch admission → provider send` 接通，先用捕获和故障注入验证实际发送字节，再做最小 live smoke。
4. **按顺序推进 0.6.2–0.6.4。** 完成开关、scope、删除/冲突生命周期和显式时间/replay。单个 offline fixture 通过，不等于所有 scope 和真实发送路径都通过。
5. **再推进 0.6.5–0.6.7。** 只扩 language 类型化槽位和最小路由遥测；先 shadow，再对通过的模型/配置做限定启用和回滚。不要因为 allowlist 能表达 command、format 或 code 就默认投递它们。
6. **0.6.8–0.6.12 保持分族 shadow。** 先研究 command 的正确宿主、provider outcome 和 Explain；World Model 只有在有真实预测/复盘消费者时才启动 0.6.12。
7. **0.7 可选扩展模型和留出任务。** 每个模型及配置独立验收；新模型未测或失败不阻塞已通过模型的生产启用。不能把单模型结果写成跨模型通过。
8. **0.8、0.9 是独立下游轨道。** Protocol Adaptation 和 Hebbian/可塑性研究都需要自己的数据、对照、停止条件；它们不能反向扩大 Ghost 的默认 prompt 权限。

如果某个实验的前置条件没有满足，就把它标记为 `BLOCKED` 或 `INCOMPLETE`，记录原因，先处理阻塞项。不要通过修改 scorer、放宽样本量或把缺失对照当作阴性来“过门”。

## 每个实验批次的固定工作流

### 开始前

- 在 `git status` 中确认没有把别人的改动混入本次工作。
- 从 Roadmap 复制相关验收门的目标和条件，写入以实验命名的进度记录；路线图版本号只作为可选的关联字段。
- 明确本次只改变一个主要变量；实验需要的模型、任务、样本量、预算、对照和停止条件先写下来。
- 如果要新增模型可见文本，先画出它经过 `ContextSource`、`PromptEnvelope` 和 admission 的路径；没有 admission 设计就不要接入普通 prompt。

### 实现和验证中

- 实现只服务当前实验或候选功能，不提前实现后续研究接口。
- 测试至少覆盖正常路径、禁用、scope、过期、冲突、删除、重建、重启和失败注入中与本版本有关的部分。
- 生产路径和 harness 分开；实验脚本可以复用数据格式，但不能成为生产 import。
- 记录选择、排位、预算、实际发送位置、拒绝原因和 digest；不要提交 raw prompt/reply。
- live 实验中发现模型服务故障时，保留原始 JSONL 和错误信息，不把故障行算作模型行为阴性。

### 完成前

必须同时有：

- 代码和行为命名测试；
- deterministic replay fixture 或等价的可重复离线证据；
- 需要模型时的 live/off 对照、模型 ID、endpoint 配置说明、样本量和报告；
- 安全边界检查：Ghost 不写 Evidence、Permission、CompletionProof，不执行工具，不绕审批；
- 回滚、禁用、隔离和删除说明；
- 本文档或进度板中的状态更新；
- 一个只包含该实验或候选功能逻辑的 Git commit 集合，并在进度记录中列出完整 hash。

## KoboldCpp 使用和故障处理

离线检查不需要 KoboldCpp。优先运行：

```powershell
python tests/manual/ghost_memory_routing/summarize.py
python tests/manual/ghost_memory_routing/round4.py --help
```

需要 live 模型时，使用隔离 suite，避免覆盖已有数据：

```powershell
python tests/manual/ghost_memory_routing/round4.py seed
python tests/manual/ghost_memory_routing/round4.py run --suite <NEW_SUITE> --endpoint <KOBOLDCPP_ENDPOINT> --model <MODEL_ID>
python tests/manual/ghost_memory_routing/round4.py report
```

规则如下：

1. 先确认 KoboldCpp 当前 endpoint、model ID、上下文长度和超时设置；报告中写明实际使用值。
2. 同一个 suite/cell 的 `run` 可能重写原始 JSONL。新批次、新模型、新量化或新配置必须使用新的 suite 名，例如 `local_model_q4_20261117`。
3. KoboldCpp 无响应、崩溃、返回格式错误、超时或被审批挂起时，先停止重试，保存时间、命令、cell、模型和错误摘要。
4. 把故障信息发给我，请我重新打开或修复 KoboldCpp；不要自行修改 scorer、降低门槛或把未完成的 cell 记成通过。
5. KoboldCpp 恢复后只重跑缺失或损坏的 cell，保留旧失败记录，并在报告中说明重跑原因。若要生成可比较的正式结果，使用新的 suite 名或新的明确运行目录。
6. 任何 live 结果都必须与离线结果分开；审批拒绝、工具回路挂起和 provider 故障分别统计，不能混成“记忆没有效果”。

给我的故障消息至少包含：

```text
实验/候选功能：
suite：
cell：
模型 ID：
endpoint：
开始时间（本地时间）：
最后一条正常输出：
错误或现象：
是否保留 JSONL / 日志：
需要我做什么：重新打开 KoboldCpp / 检查配置 / 判断是否重跑
```

## 什么时候需要找我

不需要为日常工作逐项等待确认：阅读文档、运行离线检查、更新实验记录、提交报告/脱敏指标、修复实验脚本和在 KoboldCpp 恢复后重跑明确缺失的 cell，都可以直接做。

需要找我的情况只有这些：

- KoboldCpp 需要我重新打开、换模型或检查运行配置；
- 需要改变实验任务、scorer、样本门槛、预算、对照或晋级条件；
- 需要删除原始归档、重置共享实验目录或做不可逆数据操作；
- 实验结果与已有结论冲突，无法判断是模型故障、工具回路问题还是记忆路由问题；
- 准备把一个新的生产功能从 `SHADOW_ONLY` 改为 `PRODUCTION_MERGED`，但生产合并门槛的证据还不完整。

询问时直接附上实验名称、commit、命令、报告路径和当前阻塞，不要只说“模型坏了”或“实验失败了”。

## 主代码合并门槛

实验代码、实验脚本和负结果可以保存在实验分支、`tests/manual`、`reports` 或文档中；它们默认不进入生产路径。实验结果无效时，不能把“已经写出来的实现”合并到主代码。

只有同时满足下面条件，才允许把生产代码或功能合并到主分支：

1. **有明确生产问题和收益。** 能说明它改善了用户行为、运行安全、可靠性、可观测性、成本或恢复能力中的至少一项；“架构上很有趣”不算收益。
2. **真实路径成立。** 代码经过实际 runtime / builder / admission / provider-send 中与它相关的路径验证；harness-only 结果不能作为生产合并依据。
3. **有对照和边界。** 有基线、样本量、任务范围、模型/config 范围、成本/延迟和阴性结果；缺失数据输出 `INCOMPLETE`。
   一个目标模型及配置满足上述要求即可；第二模型缺席不构成该模型的缺失数据，也不要求按其他模型的最弱结果判定。
4. **安全边界无回归。** 不绕过权限、审批、Evidence、CompletionProof、删除、scope 或当前请求优先级。
5. **可以关闭和回滚。** 有显式开关、降级/隔离路径、删除/replay 语义和失败恢复说明。
6. **结果可复现。** 另一个同事能从 commit、命令、报告和 fixture 重跑关键判断。

合并结论只允许写成以下三种之一：

- `PRODUCTION_MERGED`：生产代码/功能已通过门槛并进入主分支；
- `SHADOW_ONLY`：代码可观测但不改变默认行为，继续收集证据；
- `EXPERIMENT_ONLY`：实验无效、证据不足或暂时没有生产消费者，只保留实验材料，不合并生产实现。

这些结论按功能、模型、任务和配置范围记录；单模型通过即可正式启用，不因缺少第二模型而限定为试点或 `SHADOW_ONLY`。

## Commit 和交接记录规则

每个实验批次在阶段性结论后提交一次清晰的 commit。连续大型实验可以有多个技术 commit，但不能把未验证的实验实现混入生产 commit。建议格式：

```text
feat(ghost): add context admission shadow path
fix(ghost): close deletion replay gap
test(ghost): add placement parity battery
docs(ghost): record language shadow result
```

按当前团队流程，同事可以直接在主分支提交，不需要 PR 或等待负责人批准；因此提交边界必须更严格。实验 commit 和生产 commit 分开，不能把未验证的实验实现混入生产路径。不要把“实验代码可运行”写成生产完成；生产 commit 必须单独说明通过了哪些生产门槛。无效实验不需要生产 commit，只提交报告、负结果和必要的防回归测试。

仓库和本地归档的边界固定如下：

- **可以提交仓库**：实验脚本、测试、脱敏指标、报告、fixture、进度记录、负结果和防回归测试。
- **只留本地归档**：原始 prompt/reply、KoboldCpp 原始输出、隔离 state、完整运行日志和模型文件。
- **禁止提交**：API key、endpoint 凭据、原始用户内容、未经脱敏的模型输出，以及只有实验价值却会进入生产 import 的代码。

直接提交主分支时，建议每个阶段至少留下一个清楚的实验 commit；只有通过主代码合并门槛时，才另提交生产功能 commit。这样即使连续实验持续很久，也能从 `git log`、进度文件和报告看出每一阶段发生了什么。

每个实验或候选功能在 `docs/ghost_progress/` 建一个以实验命名的文件，例如 `docs/ghost_progress/language-admission-shadow.md`。内容至少包括：

```markdown
# Ghost 实验进度记录

- 实验名称：
- 状态：IN PROGRESS / EXPERIMENT_COMPLETE / BLOCKED / INCOMPLETE / SHADOW / ABANDONED
- 负责人：
- 开始日期：
- 最后更新：
- 关联 Roadmap 验收门（可选）：
- 实验 commit：
- 生产合并结论：EXPERIMENT_ONLY / SHADOW_ONLY / PRODUCTION_MERGED

## 本次目标

## 实际改动

## 验收命令与结果

## 实验配置

- model / suite / endpoint：
- tasks / cells / reps：
- on/off 对照：

## 结果与限制

## 已知问题和回滚方式

## 下一步
```

状态只使用以下几种值：

| 状态 | 含义 |
|---|---|
| `NOT STARTED` | 尚未开始 |
| `IN PROGRESS` | 正在实现或等待验证 |
| `BLOCKED` | 有明确外部阻塞，例如模型服务不可用或前置版本未完成 |
| `INCOMPLETE` | 已运行但缺少必需模型、对照、样本或证据 |
| `SHADOW` | 能运行并可观测，但按设计不改变默认行为 |
| `EXPERIMENT_COMPLETE` | 实验、报告和复现材料完成；不代表生产合并 |
| `PRODUCTION_MERGED` | 生产代码/功能通过生产合并门槛并已进入主分支 |
| `ABANDONED` | 证据显示没有价值或方向被替换，保留原因和数据 |

## 如何判断团队现在推进到哪里

进度不能只看“写了多少代码”，应同时看四个维度：

1. **代码状态**：实验记录、测试和 commit 是否存在；生产代码是否另有合并 commit。
2. **行为状态**：离线 replay、live/off 对照和任务指标是否通过。
3. **生产状态**：是否经过真实 admission/provider-send，是否仍是 harness-only。
4. **部署状态**：是 `SHADOW`、限定模型/配置启用，还是完整矩阵后的默认启用。

因此，实验表里的 `EXPERIMENT_COMPLETE` 不等于“Ghost 已经成功”，更不等于生产已启用。language 也必须同时写清楚“哪个模型、哪个任务、哪个投递配置可以启用”。

## 交接时必须回复的固定格式

同事直接提交主分支后，每次完成、暂停或否定一个实验，把下面这段发给团队，并把同样内容写进对应进度文件：

```text
Ghost 进度更新

实验名称：
状态：
负责人：
实验 commit：
关联 Roadmap 验收门（可选）：

本次完成：
-

验证：
- 命令：
- 结果：
- live/off 或 replay 报告：

当前生产边界：
- 是否进入真实 provider-send：
- 是否改变默认 prompt：
- 支持的模型/任务/配置：
- 生产合并结论：EXPERIMENT_ONLY / SHADOW_ONLY / PRODUCTION_MERGED

未完成或阻塞：
-

KoboldCpp 状态：
- 正常 / 需要重启 / 未使用

下一步：
-
```

最后一条原则：任何“已完成”都必须能被另一个同事从 commit、命令、报告和进度文件复现。不能复现的结果只能算线索；无效实验只能留在实验材料中，不能借路线图进度进入主代码。
