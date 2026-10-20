# Ghost / World Model 团队交接说明

> 交接日期：2026-10-21
> 基线提交：`3731c954`（Ghost 文档、路线图、实验脚本与历史快照归档）
> 适用范围：Ghost、World Model，以及它们进入 Codey runtime 的实验和生产准入工作

这份文档可以直接转发给接班同事。它是工作方法和当前判断的交接记录；路线图中的版本号只是内部验收门槛，不要求连续实验按版本号命名。具体验收条件仍以 [Roadmap](../ROADMAP.zh-CN.md) 为准，实验细节以 [Ghost 方向与晋级协议](ghost_future_direction.zh-CN.md) 和 [memory-routing 实验目录](../tests/manual/ghost_memory_routing/README.md) 为准。

## 先读这段：不要把路线图当成无条件功能清单

你接手的是一条**按证据推进、可以暂停或终止**的实验路线，不是把 Ghost 和 World Model 的所有设想逐项实现完。

连续实验按“实验批次、候选功能或研究轨道”推进，不强行拆成版本号。每个实验批次只有在实现、行为测试、deterministic replay fixture、必要的 live/offline 实验、指标报告、回滚说明和文档都完成后，才能标记为 `EXPERIMENT_COMPLETE`。代码写出来但没有证明行为、没有真实发送链路证据，状态仍然是 `INCOMPLETE` 或 `SHADOW`。

如果实验显示没有净收益，保留现有安全边界并记录阴性结果就是实验完成；无效实现不能合并进主代码。只有对生产有明确帮助、通过生产路径和安全回归的代码或功能，才允许进入主分支。

## 当前交接结论

| 项目 | 当前状态 | 交接时必须记住的边界 |
|---|---|---|
| Ghost 基础设施 | 已有可审计、可删除、可重建的本地经验存储和投影 | 这证明基础设施存在，不证明记忆一定改变行为 |
| 历史 memory-routing 实验 | 已归档并提交，含无回复测量、报告、脚本和来源摘要 | 原始回复/state 在被 Git 忽略的 `artifacts/ghost_memory_routing/`；生产代码不 import harness |
| language family | modelA、当前投递配置下是有条件的生产候选：送达 19/20，未送达 0/20 | 还没有跨模型结论，也没有完成真实 provider-send admission 验收；不能全局默认开启 |
| command / verification habit | `SHADOW` | directive 不是已证明的宿主；要区分命令建议、工具选择、审批结果和任务完成 |
| format / length / answer-first | `WEAK-HINT-ONLY` | 没有稳定的精确服从收益；列表本来就容易通过，不能算记忆收益 |
| self-description | 默认排除 | 空余名额不能回填系统自述 |
| Hebbian / Affinity | 辅助特征、关联和解释来源 | 不拥有最终投递决定权；复杂排序必须胜过简单基线才有资格扩大作用 |
| World Model | 可选 shadow 方向 | 不阻塞 language 闭环；不产生 `evidence_refs`，不裁定事实，不进入默认 prompt |

已归档实验的关键数据是：language on/off 为 `19/20` 对 `0/20`；command 主 on/off 都是 `0/20`；生产 Hebbian cap12 的 language 为 `6/10`，分层 cap12 为 `9/10`，新近 cap12 为 `2/10`。这些数字只支持受测模型和配置下的条件结论，不能把它们改写成“Ghost 已经普遍有效”。

## 推荐接手顺序

默认从路线图中第一个未完成的验收门开始，不能直接跳到 World Model 或大规模学习机制。接班记录使用实验名称，不要求把每次连续实验写成新的版本号。

1. **先确认工作区和基线。** 阅读本文、[Roadmap](../ROADMAP.zh-CN.md)、[Ghost 方向](ghost_future_direction.zh-CN.md)，运行现有的无模型复查；确认当前分支、Python 环境、测试状态和 `git status`。
2. **先做 0.6.0。** 冻结必测模型、任务、on/off 对照、最低样本量、scorer、收益门槛和缺失数据规则。缺模型、缺 off、缺样本必须输出 `INCOMPLETE`，不能按 0% 计算。
3. **再做 0.6.1。** 把真实 provider-send 路径上的 `ContextSource → PromptEnvelope → Context Epoch admission → provider send` 接通，先用捕获和故障注入验证实际发送字节，再做最小 live smoke。
4. **按顺序推进 0.6.2–0.6.4。** 完成开关、scope、删除/冲突生命周期和显式时间/replay。单个 offline fixture 通过，不等于所有 scope 和真实发送路径都通过。
5. **再推进 0.6.5–0.6.7。** 只扩 language 类型化槽位和最小路由遥测；先 shadow，再对通过的模型/配置做限定启用和回滚。不要因为 allowlist 能表达 command、format 或 code 就默认投递它们。
6. **0.6.8–0.6.12 保持分族 shadow。** 先研究 command 的正确宿主、provider outcome 和 Explain；World Model 只有在有真实预测/复盘消费者时才启动 0.6.12。
7. **0.7 才扩第二模型和留出任务。** 完整矩阵按最弱必测模型收敛；不能把第二模型缺席时的 modelA 结果写成跨模型通过。
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
python tests/manual/ghost_memory_routing/round4.py run --suite modelB --endpoint <KOBOLDCPP_ENDPOINT> --model <MODEL_ID>
python tests/manual/ghost_memory_routing/round4.py report
```

规则如下：

1. 先确认 KoboldCpp 当前 endpoint、model ID、上下文长度和超时设置；报告中写明实际使用值。
2. 同一个 suite/cell 的 `run` 可能重写原始 JSONL。新模型、新量化或新配置必须使用新的 suite 名，例如 `modelB_q4`。
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
4. **安全边界无回归。** 不绕过权限、审批、Evidence、CompletionProof、删除、scope 或当前请求优先级。
5. **可以关闭和回滚。** 有显式开关、降级/隔离路径、删除/replay 语义和失败恢复说明。
6. **结果可复现。** 另一个同事能从 commit、命令、报告和 fixture 重跑关键判断。

合并结论只允许写成以下三种之一：

- `PRODUCTION_MERGED`：生产代码/功能已通过门槛并进入主分支；
- `SHADOW_ONLY`：代码可观测但不改变默认行为，继续收集证据；
- `EXPERIMENT_ONLY`：实验无效、证据不足或暂时没有生产消费者，只保留实验材料，不合并生产实现。

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
