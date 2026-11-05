# Codey 内核地毯式精简审计（2026-11-04）

## 目的

用与 `research-complete-branch-review-20261103.zh-CN.md` 相同的证据纪律，审计 Codey 的共享任务内核：

- `operations`
- `runtime`
- `durable`
- `providers`
- `app`
- 以及 Research、Project、Chat、Ghost 等消费者与边界

问题不是单纯统计代码行数，而是逐项判断：

1. 这部分是否给模型直接或间接带来收益；
2. 这部分是否是权限、持久化、恢复、幂等或数据一致性所必需；
3. 是否存在重复职责、死分支、过重的上下文投影或过多的状态转换；
4. 删除或简化后，模型行为、系统边界和恢复能力是否退步；
5. 是否能用更简单的实现保持同样行为，或让模型少走轮次、少用输入 token、减少无效调用并提高完成准确性。

## 实验隔离

- 原版对照树：`D:\codey`
- 原版分支：`master`
- 实验树：`D:\codey-kernel-audit`
- 实验分支：`audit/kernel-audit-20261104`
- 起始提交：`857d8570813f027a0bc28f0c92fd49bada85b1ec`
- 起始状态：原版工作区干净；实验树从该提交创建
- 当前阶段：只读盘点与基线准备，尚未修改生产代码

实验候选按独立变体处理。原版作为 A，对照变体作为 B；不在原版工作树直接试删。

## 判断规则

### 可以保留

- 有可重复的模型收益；或
- 是明确的系统安全/一致性边界；或
- 删除后会破坏旧数据、恢复、权限、幂等或现有消费者。

### 可以合并

- 逻辑确实需要，但有多个实现或多个重复投影；
- 合并后行为、边界和恢复结果保持一致；
- 确定性回归和实机对照均通过。

### 可以简化

- 能减少状态、分支、上下文或重复计算；
- 没有放宽权限、版本、引用、写入、恢复和取消边界；
- 模型任务结果不退步，最好有轮数、token、准确性或延迟收益。

### 可以删除

只有在同时满足以下条件时才考虑删除：

- 没有生产消费者，或已证明消费者不需要它；
- 不承载持久化兼容、恢复、权限或幂等不变量；
- 确定性回归覆盖了原能力边界；
- 实机任务没有退步；
- 删除后的职责地图更简单且不会留下隐式替代路径。

## 证据等级

| 等级 | 证据 | 能说明什么 |
|---|---|---|
| E0 | 文件名、行数、命名直觉 | 只能提出候选，不能决定删除 |
| E1 | 静态调用、注册表、导入和消费者追踪 | 说明生产路径和边界候选 |
| E2 | 确定性单测、故障注入、恢复和合同测试 | 说明结构行为和安全边界 |
| E3 | 固定模型、固定材料、固定 seed 的真实 A/B | 说明模型请求和任务表现差异 |
| E4 | 新 seed、留出题、扩大回归和人工内容核查 | 支持是否合入生产的判断 |

“模型没有直接看到”不等于“系统可以删除”。durable、effect、权限和恢复要单独按 E2 判断。
“测试通过”也不等于模型语义正确；模型收益必须按 E3/E4 判断。

## 固定比较指标

### 模型和任务指标

- 正式完成率
- 必要事实和条件是否保留
- 引用、版本和对象是否正确
- 首次回答与最终回答是否发生语义漂移
- 无效工具调用次数
- 修复次数和重复动作
- 模型请求轮数
- 输入 token、输出 token
- 任务耗时

### 系统边界指标

- 旧数据读取和冷启动恢复
- 已结算 effect 是否重复执行
- 写入和发布是否幂等
- 权限是否扩大
- 取消、超时、未知 provider 结果的处理
- 当前版本、来源版本和引用绑定
- 不同 provider 与文本/native 协议是否保持合同

### 结构指标

- 生产消费者数量
- 跨包依赖和循环
- 重复状态、重复投影和重复计算
- 大模块职责数量
- 删除/合并后的代码量与边界测试数量

## 工作阶段

### 阶段 0：冻结基线

记录当前提交、工作区、Python/provider 配置、已有测试入口、模型连接和可用实机 harness。

### 阶段 1：建立内核生命周期图

从用户输入开始，追踪策略、上下文、provider、工具、effect、durable、completion、恢复和 UI 投影。
每个节点记录所有者、输入、输出、持久化事实、模型可见性和失败处理。

### 阶段 2：建立候选清单

按“模型输入与协议、执行与 effect、持久化与恢复、provider、app 组合、跨任务兼容”分组。
每个候选先静态核对，再决定是否进入实验。

### 阶段 3：确定性验证

先在实验树执行定向合同、恢复、权限、幂等、取消和协议测试。遇到失败，区分生产代码问题与测试/夹具落后。
不为了过测试而放宽生产合同。

### 阶段 4：实机 A/B

原版 A 与单个实验变体 B 使用同模型、材料、seed、预算和入口。保留 wire、请求历史、工具调用、结果、数据库事实和人工内容核查。

### 阶段 5：扩大验证与分批提交

对有收益的候选增加新 seed 和留出任务，运行相关扩大回归、Ruff、Mypy、Pyrefly、ty 及差异检查。
每个已确认候选单独提交；未通过的候选保留失败证据，不进入主线。

### 阶段 6：更新报告与合并建议

每个候选写明保留、合并、简化、删除或暂缓，以及证据边界。只有通过门槛的实验变体才提出合并到 `master`。

## 当前记录

### 2026-11-04：审计启动

- 用户确认开始对 Codey 共享内核做地毯式审查。
- 用户要求保留原版对照、使用 Git 独立实验树、分步记录、实机 A/B、扩大测试、修复真实生产问题、更新文档并分批提交。
- 创建实验分支 `audit/kernel-audit-20261104` 和 worktree `D:\codey-kernel-audit`。
- 原版 HEAD 为 `857d8570`；起点文档提交后，实验树将有第一笔仅记录审计起点的提交。
- 尚未修改任何生产逻辑。

## 后续记录格式

每完成一个审计动作，追加：

- 日期和阶段
- 具体范围
- 观察到的事实
- 证据文件或命令
- 当前判断
- 尚未证明的部分
- 下一步
- 对应提交或实验产物

## 阶段 0 结果：基线冻结与第一批验证

### 机器盘点

产物：`.e2e-artifacts/kernel-audit-20261104/static-inventory.json`。

当前实验树以 `857d8570` 为生产基线，盘点到选定生产包的 **496 个 Python 模块、132,470 行、6,622 个定义**（定义总数由产物保存；其中包含共享内核及其 Research/Ghost/Knowledge/Workspace 消费者）。主要包规模如下：

| 包 | 文件 | 行数 |
|---|---:|---:|
| `operations` | 75 | 19,742 |
| `providers` | 73 | 16,969 |
| `app` | 52 | 12,577 |
| `runtime` | 48 | 11,038 |
| `ghost` | 33 | 13,339 |
| `durable` | 29 | 8,738 |
| `research` | 76 | 18,856 |

初步结构候选包括：`durable/session.py`（2196 行）、`workspace/changes.py`（1415 行）、`providers/controls.py`（1384 行）、`runtime/effects/tool_result_delivery.py`（1252 行）、`app/context.py`（1164 行）、`durable/model.py`（1013 行）、`runtime/effects/effect_records.py`（995 行）、`toolchain/tool_spec.py`（994 行）。这些只是复杂度和耦合候选，不是删除结论。

扇入最高的共享位置包括 `durable.model`、`runtime.core.models`、`storage.local_store`、`providers.base`、`providers.diagnostics`、`operations.task_state`、`toolchain.tool_spec` 和 `runtime.effects.effect_records`。它们一旦变更会影响大范围消费者，后续需要优先做职责和事实所有权核对。

本次精确模块导入图没有把包级别延迟导入直接当作运行时循环；后续会另外做规范化导入图和动态边界检查，避免把类型导入或延迟导入误报成架构问题。

### 确定性基线

基线选集共收集 **187 个测试用例，187 passed，0 failed**，耗时 83.30 秒，覆盖：

- 有限 effect 交付状态和能力集合不变量；
- 恢复不改变事实且不重复执行；
- 原始策略恢复、权限和任务入口；
- 内核依赖方向和单一 typed request 边界；
- 快照、收据完整性、协议修复、结算失败 fail-closed；
- Task kernel 统一入口和生产调用方。

检查命令和完整节点保留在本次工作记录中；没有运行全量 pytest。

### 静态质量基线

| 检查 | 结果 |
|---|---|
| 目标包 Ruff | 通过 |
| Mypy，Python 3.11 | 通过，575 个源文件 |
| Pyrefly，Python 3.11 | 0 errors，35 suppressed，79 warnings |
| ty | 通过 |
| 原版工作区 | 保持干净 |

### 阶段判断

1. 当前内核已有较强的确定性边界，不能用“代码很多”直接证明可以删。
2. 共享事实模型和 effect/恢复模块是高风险区域，先做所有权和消费者审计，再做简化实验。
3. Research 报告中的模型请求投影问题仍值得作为共享 kernel 的第一批模型实验候选，但要和 durable/effect 安全边界分开。
4. 当前还没有实机 A/B 结果；不能把确定性测试全绿写成模型收益。

### 下一步

建立“用户任务 → 入口策略 → prompt/context → provider → tool/effect → durable settlement → completion → recovery/UI”的调用和事实所有权图，并把每个节点标记为：模型可见、模型间接、安全底座、兼容分支或展示投影。


## 阶段 0 实机基线：共享内核合同与恢复

产物目录：`.e2e-artifacts/kernel-audit-20261104/live-baseline-contract/`。

使用当前实验树未改生产逻辑的版本，连接本机 KoboldCpp：

- 模型：`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`
- endpoint：`http://127.0.0.1:5001/v1`
- temperature：0.3
- seed：41
- 协议：native/on 与 text/off
- 任务：读取两个文件并返回观察值；创建指定文件并报告实际写入

首次直接执行脚本时，`tests/manual/contract_consistency_live.py` 没有把仓库根目录加入导入路径，出现 `ModuleNotFoundError: No module named 'tests'`。这是手工测试入口接线问题，不是生产代码失败；保留该失败记录，随后使用模块入口重新运行，没有修改生产代码。

模块入口重跑结果：

| 协议 | 任务 | 结果 | 模型请求 | 秒数 | 恢复完成 | 重复恢复 |
|---|---|---:|---:|---:|---:|---:|
| native/on | read | 通过 | 2 | 10.64 | 通过 | 稳定 |
| native/on | write | 通过 | 2 | 8.55 | 通过 | 稳定 |
| text/off | read | 通过 | 2 | 11.75 | 通过 | 稳定 |
| text/off | write | 通过 | 2 | 8.56 | 通过 | 稳定 |

合计 **4/4 通过、8 次模型请求**。四例均满足工具合同、实际文件/读取结果、完成判定、恢复后完成判定和重复恢复不增加事实。该基线说明当前共享内核在这组受控任务上可运行；样本只有一个 seed，不能证明通用模型收益，也不能证明所有 provider、任务类型和故障路径。

## 阶段 0 当前结论

- 原版对照已冻结，实验树与原版隔离。
- 确定性边界选集 187/187 通过。
- 目标包质量检查通过。
- 本地 Gemma 12B 的 read/write、native/text 受控基线 4/4 通过。
- 第一组候选不能从“当前能跑”推导删除；下一步必须找出真实进入模型请求的共享路径，再对单一候选做 A/B。
- 测试入口失败与生产失败已分开记录；后续测试报错继续按这个规则处理。


## 阶段 1：共享内核生命周期与事实所有权地图

机器地图：`.e2e-artifacts/kernel-audit-20261104/kernel-lifecycle-map.json`。

当前主链可以压缩成以下顺序：

```text
提交入口
  → run setup / task mode dispatch
  → TaskSession + TaskPolicy + KernelRunRequest
  → 唯一 run_task_kernel
  → KernelContext 准备 prompt/context、解释回复、执行工具
  → provider request / tool effect
  → RuntimeEffect intent → MutationLine → DurableSession settlement
  → completion gate
  → answer / trace / UI projection

重启：DurableSession + receipt artifacts
  → recovery 校验原 policy、snapshot、effect 和结果
  → 恢复 TaskSession 事实
  → 继续同一个 kernel，不重复已结算效果
```

### 节点分类

| 分类 | 当前节点 | 判断原则 |
|---|---|---|
| 模型可见 | `kernel_prompt`、`kernel_preparation`、`kernel_context`、Research `answer_context`、工具合同 | 需要实机 A/B 判断是否减少模型错误、轮次和 token |
| 模型间接 | `kernel_driver`、`kernel_execution`、provider、工具适配器 | 看工具结果、协议、预算和失败是否影响模型行为 |
| 安全底座 | `runtime/effects`、`mutation_line`、`durable`、completion gate、receipt recovery | 先用故障注入和确定性测试验证，不能因模型看不见就删除 |
| 编排边界 | `task_submit`、`task_run`、`dispatch`、`task_entry`、app context | 核对不同消费者是否真的需要各自分支，寻找重复装配 |
| 展示投影 | runtime observe、run trace、app query/UI | 应是事实的只读投影，不能反向成为第二套状态 |

### 当前事实所有者

- `TaskPolicy` 和冻结授权由任务入口及运行时状态保存；模型计划不能扩大授权。
- `TaskSession` 持有当前轮可用的任务事实和工具结果投影。
- `KernelRunRequest` 是共同内核的单一依赖边界，分 transport、execution、observation 三类资源。
- `RuntimeEffectIntent/Settlement` 记录外部效果身份和结果；同名工具或同参数不能替代效果身份。
- `RuntimeMutationLine` 是运行时写入口，`DurableSession` 是持久提交和恢复的事实底座。
- completion gate 组合最终完成证明；模型的 `done` 只是候选。
- observe/UI 只能读取投影，不能写回任务事实。

### 第一轮需要验证的矛盾

1. **共同内核是否真的共同**：四个生产消费者进入 `run_task_kernel`，但入口前后的装配、task kind 和完成要求差异很大；要检查这些差异是必要边界还是重复编排。
2. **模型输入是否混入太多运行事实**：`kernel_context` 会在每次准备轮次时组合任务、上下文、召回、指导和工作视图；要用 wire 记录确认模型真正看到的内容以及重复部分。
3. **effect 和 durable 是否重复承担相同职责**：当前两层都保存 intent/settlement/receipt 相关事实；先验证它们的所有权和恢复需求，不能按名称判断重复。
4. **provider 特化是否泄漏进共享内核**：Local、API、Zen 有各自连接和协议层；要检查共享内核是否仍在猜 provider 行为或保留不必要的兼容分支。
5. **完成门和后续投影是否重复检查**：完成证明、trace、Research/Project 后续处理可能各自重新读取事实；要区分必要的不同视图与重复计算。
6. **大模块是否只是组织问题，还是职责真的重叠**：先列出每个模块的输入/输出和生产调用者，再决定拆分、合并或保持。

### 阶段判断

生命周期地图确认了审计对象：不是把整个 `codey` 当作一个黑箱，而是逐节点验证模型输入、外部效果、持久事实和完成证明的所有权。下一步从模型可见的请求投影和协议修复开始，因为它们最可能同时影响模型收益和内核复杂度；安全底座随后用独立故障矩阵审计。

## 阶段 0 扩大实机基线：多 seed、协议和任务类型

产物目录：`.e2e-artifacts/kernel-audit-20261104/live-baseline-expanded/`。

固定模型、温度和入口，新增 seed 74、75，覆盖 native/on 与 text/off，以及 `read`、`write`、`replace`、`search_literal`、`search_context` 五类任务。

结果为 **20/20 通过、48 次模型请求**：

| 协议 | 任务 | seed 74 请求 | seed 75 请求 | 结果 |
|---|---|---:|---:|---:|
| native/on | read | 2 | 2 | 2/2 |
| native/on | write | 2 | 2 | 2/2 |
| native/on | replace | 3 | 3 | 2/2 |
| native/on | search_literal | 6 | 2 | 2/2 |
| native/on | search_context | 2 | 2 | 2/2 |
| text/off | read | 2 | 2 | 2/2 |
| text/off | write | 2 | 2 | 2/2 |
| text/off | replace | 2 | 4 | 2/2 |
| text/off | search_literal | 2 | 2 | 2/2 |
| text/off | search_context | 2 | 2 | 2/2 |

所有案例均通过实际文件/搜索结果、工具合同、完成判定、恢复完成判定和重复恢复稳定性检查。native seed 74 的 `search_literal` 出现连续重复搜索，text seed 75 的 `replace` 出现两次 CodeMode 编译错误后恢复；它们仍完成，但说明模型轮数和修复成本具有自然波动。

这组基线暂时支持三点：

1. 当前共享内核在小型读写、搜索和版本化编辑任务上行为稳定。
2. “少一轮”必须在多 seed 上比较，单例下降不足以证明收益。
3. 后续候选需要记录首次正确响应、修复响应和最终响应，特别是不能只看最终 `ok=true`。

## 阶段 1：模型请求投影审计（2026-11-04）

### 观察到的重复

机器产物：`.e2e-artifacts/kernel-audit-20261104/kernel-request-projection-20261104.json`。

从扩大实机基线的 20 个首轮请求逐个读取实际 wire。当前 coding/project 任务的首轮请求同时带有：

1. `Current work observations (runtime records)`：运行时事实、工作区身份、已读/已改文件、执行与验证回执、权限和待完成检查；
2. `User task (verbatim)`：完整任务原文、工作区、任务类型、同一组完成要求和工具合同；
3. 请求上下文；
4. native 模式的工具 schema，或 text 模式的长工具合同 system 消息。

20/20 个请求中，运行时观察 JSON 的 `user_task` 字段与同一次请求的 `User task (verbatim)` 重复。首轮用户消息字符数为：native **4,011–5,829**，text **13,420–14,470**；text 的长 system 合同和 native 的工具 schema 还分别占据额外输入。KoboldCpp 报告的首轮 prompt token 为 **3,575–3,890**，不能把字符差直接当成 token 节省，因为工具 schema、system 合同和 tokenizer 也在成本中。

这说明存在一个具体的奥卡姆候选：`working_context(session)` 不必再次携带 `user_task` 字段。它只是在运行时事实块中重复任务锚点；任务原文仍由冻结的 `kernel_prompt_for_session` 携带，任务事实和完成要求仍由 `TaskSession`、`TaskPolicy`、required checks 及持久化快照持有。

### 不能直接删整块

`Current work observations` 不是纯展示文字。它还承担模型看到当前工作状态的入口，包含工作区指纹、执行收据、验证是否过期、权限和待执行检查。删除整个块会丢掉完成门所需的模型可见事实，也会影响错误恢复和审批后的连续请求。因此第一候选只允许删除其中重复的 `user_task` 成员；不改变 TaskPolicy、工具 schema、effect、durable、completion 或 recovery。

### 候选 K-P1：移除观察块中的重复 `user_task`

这是一个隔离实验候选，尚未进入生产：

- **变更位置**：`codey/operations/kernel_prompt.py::working_context` 的 JSON 投影；必要时只更新断言该字段的测试；
- **保持不变**：任务原文、任务类型、工作区、权限、执行/验证回执、required checks、工具合同、协议修复、持久化格式和外部 API；
- **假设**：模型在获得同一任务原文和运行事实后，不需要第二份任务字符串；输入会略减，任务执行和完成判断不退步；
- **风险**：某些恢复或审批路径可能只把工作观察块重新放入请求，导致模型失去任务锚点；测试也可能把该字段当作任务身份诊断来源；
- **回滚条件**：任何任务原文缺失、权限/完成要求丢失、恢复提示无法解释，或实机任务完成率、首次正确响应、修复次数、请求轮数出现退步，立即撤回候选；
- **证据边界**：当前只有 KoboldCpp Gemma 12B 的小型 coding/project 任务证据，不能外推到 Research、Ghost、其他 provider 或长上下文任务。

### K-P1 的验证顺序

1. 先运行 prompt rendering、kernel protocol、task entry、completion、recovery 和 shell approval 相关定向测试，确认只有重复字段消失；
2. 再用 seed 41、74、75 和原先相同的 read、write、replace、search_literal、search_context 任务做 A/B；
3. 对每个请求保存首轮/后续 prompt token、请求轮数、首次正确响应、协议修复、工具调用、最终文件与答案、恢复完成和重复恢复稳定性；
4. 先看是否保持行为，再看输入 token 或轮数是否稳定下降。单个 seed 少一轮不能合并；
5. 若通过，再增加留出任务和 provider/任务类型，运行相关质量检查并单独提交；若未通过，保留失败产物，判断是候选设计、测试接线还是生产代码问题。

### 当前判断

K-P1 是“可以验证的局部重复”，还不是“应该删除的代码”。目前没有生产代码改动，也没有模型收益结论。下一步是把候选做成实验树中的单变量变体，并先跑定向确定性测试。

## 阶段 1：K-P1 实验结果（2026-11-05）

实验树中的候选变体只在外层 prompt 已经携带完整 `User task (verbatim)` 时，把 `working_context` 的重复 `user_task` 成员省掉；结果交付、审批、恢复和后续连续请求仍保留任务锚点。没有改持久化格式、工具合同、权限、effect、durable 或 completion。

### 确定性结果

定向选择集：prompt rendering、任务输入/输出、工具结果、shell approval、运行时回执、API request context、prepared provider request、task entry、kernel recovery 和协议修复，共 **154 passed、6 subtests passed**。首次直接删除字段时，测试暴露出后续观察请求确实需要任务锚点；因此候选收窄为“仅外层 prompt 已有原任务时省略”，并增加 `include_task` 的明确回归断言。

### 实机结果

机器产物：

- 原版 A：`.e2e-artifacts/kernel-audit-20261104/live-baseline-expanded/`；
- K-P1 B：`.e2e-artifacts/kernel-audit-20261104/live-kp1-no-observed-task/`；
- 配对比较：`.e2e-artifacts/kernel-audit-20261104/kernel-kp1-ab-comparison.json`。

固定 KoboldCpp、Gemma 12B、temperature 0.3、native/text 两种协议，覆盖 seed 41、74、75 以及 read、write、replace、search_literal、search_context：**30/30 通过、66 次模型请求**。与原 A 共同的 seed 74/75 共 20 个配对全部通过；恢复完成、重复恢复稳定、工具合同和最终文件/答案均通过。

共同 20 个配对的成本变化：

| 指标 | A 原版 | B K-P1 | 变化 |
|---|---:|---:|---:|
| 首轮 prompt token 合计 | 74,380 | 73,028 | −1,352（−1.82%） |
| 首轮平均每例 | 3,719 | 3,651.4 | −67.6 |
| 全部 prompt token 合计 | 192,887 | 168,094 | −24,793（−12.85%） |
| 模型请求数 | 48 | 44 | −4（−8.33%） |

全量 prompt 的下降有一部分来自请求轮数变化，因此不能把 −12.85% 全部归因于字段删除。首轮下降较稳定，约随任务文字长度减少 35–121 token。请求轮数有方向相反的个例：某些搜索题少了多次重复调用，另一个 text/search_context 配对多了一轮；这说明模型行为仍有波动，不能仅凭总数宣布收益。

### K-P1 当前处理

在这组受控 coding/project 任务上，K-P1 没有发现行为退步，并显示稳定的首轮输入下降信号；但还没有证明跨任务、跨 provider 或长上下文普遍收益。候选暂列 **保留在实验树、暂不合并 master**。下一步是运行更广的共享 kernel 确定性回归和质量检查，再用 Research/混合任务及留出题确认它没有把任务要求、研究要求或恢复提示藏掉。

### K-P1 收尾验证

- 定向扩大回归：**229 passed、6 subtests passed**；没有运行全量 pytest；
- Ruff：全仓通过；
- Mypy：`codey` **575 个源文件**通过；
- Pyrefly：**0 errors**，既有 35 suppressed、79 warnings；
- ty：通过；
- `git diff --check`：通过。

实验中第一次把字段从所有观察块移除时，154 项选择测试中的 5 项暴露任务锚点退步；该版本未保留。修正为只在外层已有完整任务 prompt 的初始投影中省略后，229 项回归和 30 次实机任务均通过。这是候选设计被边界证据修正的记录，不能把失败版本的结果混入 K-P1 的收益统计。

## 阶段 2：durable 与 runtime effect 所有权审计（2026-11-05）

机器产物：`.e2e-artifacts/kernel-audit-20261104/durable-effect-ownership-20261105.json`。

这两个区域名称相似，但目前没有证据表明它们在保存同一份“第二数据库”：

| 区域 | 真正拥有的职责 | 能不能互相替代 |
|---|---|---|
| `durable.model.EffectRecord`、`DurableSession`、`DurableStore` | SQLite 中的 effect intent/settlement、`pending_effect_ids`、CAS、幂等提交、进程重启后的权威事实 | 不能由 runtime log 替代；它决定任务是否还能安全推进 |
| `runtime.effects.effect_records.RuntimeEffect*` | 对 provider/tool effect 的严格语义校验、replay class、调用身份、结果字段和 recovery summary | 不能直接塞进 storage model；它是运行时规则和纯投影 |
| `durable.checkpoint` | 把 runtime projection 转成 durable mutation，并从 durable facts 重建 runtime session view | 是适配层，不是第三套事实 |
| `runtime.log` / session view | 面向运行和诊断的重建视图 | 不能决定外部 effect 是否执行 |

关键路径是：

```text
runtime projection
  → durable.checkpoint._effect_changes
  → DurableSession._commit / SQLite EffectRecord
  → checkpoint_entries
  → runtime session view / recovery
```

当前代码明确把 durable effect 作为恢复来源；`RuntimeEffectStore` 只是从 durable records 读取并严格解码，注释也明确 runtime projection 不能决定 effect 是否运行。这样分层是有原因的：storage 需要通用、可迁移、可比较的记录；runtime 需要拒绝未知字段、错配 call id、错误 settlement、危险 replay 和不完整结果。

### 确定性验证

effect projection、runtime effect records、effect identity、durable recovery、not-sent continuation、single-settlement fail-closed、projection boundary、crash recovery 等定向选集共 **128 passed、2 skipped**。

这些测试确认：

- 没有 intent 就不能 settlement；
- 相同参数仍必须用不同 effect identity，不能用 args digest 冒充调用身份；
- 同一 settlement 重放不重复推进 durable revision，不同结果不能覆盖既有事实；
- pending effect 会约束恢复，只有显式安全的读操作可以重试；
- runtime JSONL/诊断投影不参与恢复权威判断；
- tool/provider 分类、结果收据和任务 checkpoint 在同一 durable 提交边界内保持一致。

### 当前判断

这一层暂不产生删除候选。删掉 durable 层会让“已执行但未确认”的边界失去持久权威；删掉 runtime 层则会把复杂语义校验、重放策略和模型可见结果恢复塞回 storage，职责反而更混乱。下一步只继续查更小的疑点：`recovery_summary` 的分支是否都有真实消费者，以及适配层是否有字段只写不读；在没有使用证据前不改 effect/durable 结构。

### 小疑点：`interrupted_repairs`

静态搜索显示 `RecoverySummary.interrupted_repairs` 只在默认值、`recovery_summary()` 的局部初始化、`runs/details.py` 的转发判断和复制中出现；当前生产代码没有任何地方把它增加到非零，也没有持久化字段或用户界面行直接展示它。它很像早期“修复轮次也是 effect”的遗留接口。

按奥卡姆原则，它可以成为删除候选；按当前兼容规则，它暂时不能直接删除：`RecoverySummary` 是可导入的运行时类型，外部代码可能手工构造它，虽然仓库内没有这样的生产调用。当前处理是 **保留字段、记录为疑似死分支**，不把“仓库内没有写入者”当作“外部 API 已证明不存在”。如果后续决定清理，需要先做版本兼容策略和 UI/API 对照，再单独提交。

## 阶段 3：providers 与共享 kernel 边界审计（2026-11-05）

### 当前 provider 分工

静态调用和 provider contract 测试显示，共享 kernel 没有直接猜“某个模型会怎样”，而是先冻结一次协议判断，再按 provider 合同发送：

```text
provider adapter chain
  → provider_uses_native / supports_native_tools
  → native: send_turn / send_tool_results / bound window / acknowledge
  → text: send / text contract / result spans / repair
  → provider-owned codec, context ledger, request admission and response decode
```

目前这些分支分别守不同边界：

- `supports_native_tools` 处理真实 provider 能力、显式环境覆盖、local 默认值和测试 Mock 的误报；
- `ApiProvider` 负责 OpenAI completions/responses、native/text 编码、请求上下文、结果 span、窗口恢复和最后一次 HTTP admission；
- `ZenProvider` 把 Zen runtime 的原生工具名映射回共享工具合同，并维护自己的连接/窗口生命周期；
- local 配置负责 `auto/on/off/json` 的持久选择和 capability snapshot；
- `kernel_transport` 只负责沿 adapter 链找到最终 provider，并在协议识别失败时 fail closed。

因此 `send`、`send_turn`、`send_tool_results`、bound result delivery 和 `acknowledge` 不是重复的同义入口：它们分别对应 text/native、冷窗口/绑定窗口、结果回执和关闭确认。合并成一个“万能 send”会重新把 call id、窗口身份和恢复边界交给隐式分支。

### 确定性和实机证据

provider、协议窗口、native call id、text repair、responses history、local mode、Zen adapter 和 provider durable receipt 选集共 **170 passed、64 subtests passed**。此前 KoboldCpp baseline 和 K-P1 A/B 也同时覆盖 native/on 与 text/off；两组的工具合同、恢复和最终结果均通过。

当前没有发现可以安全删除的 provider 分支。`request_scoped_tool_context`、`text_tool_format` 等属性式 capability 是后续可研究的结构简化候选：它们现在通过 `getattr` 进入 kernel，若改成一个冻结的 typed capability snapshot，可能减少猜测和重复判断，但属于接口重构，不能和本轮删除实验混在一起。

### 当前判断

providers 层暂时 **保留现状**。下一步若要继续精简，应先对 capability snapshot 做只读静态盘点：列出每个属性的生产写入者、读取者、持久化/恢复位置和真实 provider 覆盖范围；没有确认某属性只服务一个已退出协议前，不删除兼容字段。

## 阶段 4：operations/app 共享消费者审计（2026-11-05）

共享 kernel 目前有四类真实生产消费者：

| 消费者 | kernel 之前负责什么 | 为什么不能直接合并成同一入口 |
|---|---|---|
| `task_entry` | durable 任务入口、原策略恢复、Ghost/Project/Research 资源装配、最终任务结算 | 它是正式提交入口，必须先恢复原授权并绑定同一 durable task |
| `project_adapter` | 兼容 `AgentRequest`/`RunResult`、项目上下文、review/repair 外部流程 | 它保留旧的公开调用合同和项目后处理，不能把旧调用方直接改成 durable entry |
| `research_continuation` | 研究 invocation 次数、已结算结果复用、handoff 回执恢复 | 它可能直接返回已完成 invocation，不能每次重新跑 kernel |
| `project_audit_advisor` | 独立只读审计任务、独立 provider deadline、自己的 durable/effect 记录 | 它是隔离的 advisor run，不能混入主项目任务的授权和收据 |

这四条路径最后都进入 `run_task_kernel`，共享工具执行、协议修复、effect settlement 和 completion gate；入口前的差异分别对应授权、兼容、恢复和隔离边界。当前看到的重复主要是 policy/session/context 的装配代码，不是第二个模型循环。

### 确定性验证

task entry、task kernel unification、Project audit、Research whole/continuation、typed request、旧 facade 清理和 Project completion 选集共 **247 passed、6 subtests passed**。测试覆盖了原策略恢复、共享 session、审计独立任务、研究续作不重复执行以及项目后处理。

### 当前判断

operations/app 这一层暂不删除入口。可以研究的结构候选是把四类入口的“装配结果”统一成一个 typed `KernelRunRequest` builder，同时保留各自的外部合同和恢复策略；这会减少重复，但属于可回滚重构，必须先画出每个字段的所有者，再做单变量实验。当前没有证据支持直接删掉 `project_adapter`、`research_continuation` 或 `project_audit_advisor`。

## 阶段 5：capability snapshot 审计（2026-11-05）

机器产物：`.e2e-artifacts/kernel-audit-20261104/capability-snapshot-audit-20261105.json`。

这里实际有三层“能力信息”，不能把它们当成重复字段：

| 层 | 代表对象 | 作用 | 是否持久化 |
|---|---|---|---|
| 静态 fallback 能力 | `ProviderCapability` | provider 排序、local 默认 native 模式、保守 fallback | 代码静态表 |
| 本次运行的冻结合同 | `ApiRunSelection` | connection/model/protocol、native/text、工具格式、上下文预算、tokenizer、generation 参数 | durable `provider_snapshot.model_selection` |
| 实时协议状态 | provider 实例属性 | `window_ref`、context ledger、当前 codec、request-scoped context 等真实连接状态 | provider 自己的窗口和 durable request context |

审计结果：

- 静态 capability 不能替代本次运行的 selection，否则恢复时可能把模型或协议换掉；
- `window_ref` 是一次协议窗口的身份，不是简单的 true/false capability；
- `request_scoped_tool_context` 目前是 `ApiProvider` 上的隐式能力标记，kernel 通过 `getattr` 读取；
- local 的 `auto/on/off/json` 选择和 KoboldCpp 的上下文预算必须保留，不能因为静态表已有 `native_tools_default` 就跳过实际选择；
- 当前没有字段被证明“只写不读”或可以在保持 durable snapshot 兼容的前提下删除。

### 确定性验证

provider snapshot、API selection 持久化、上下文预算 admission、本地模式、协议窗口和 prepared request 选集共 **99 passed、31 subtests passed**。

### 当前判断

capability 层没有直接删除候选。可以研究一个结构候选：从已冻结的 `ApiRunSelection` 和 provider 实时状态生成一个 typed capability view，再传给 `KernelContext`，减少 `getattr` 和重复判断；但必须保留现有 snapshot 字段、`window_ref` 身份及注入 provider 的兼容入口。这个候选暂不实现，先进行入口装配字段对照和留出任务验证。

## 阶段 6：入口装配字段对照（2026-11-05）

机器产物：`.e2e-artifacts/kernel-audit-20261104/entry-assembly-field-map-20261105.json`。

把四个 `run_task_kernel` 调用点的 `KernelTransportDeps`、`KernelExecutionDeps` 和 `KernelObservationDeps` 字段逐项列出后，确实有一组重复装配：`provider`、`run_id`、`effect_scope`、`provider_id`、`user_task`、`intent_sink`、`on_boundary`、`completion_context` 等在多个入口出现。但差异也是真实的：

- `task_entry` 额外负责 Ghost admission、attempt gate、workspace writer、workspace revision 和完整 durable 资源；
- `project_adapter` 需要保留 `AgentRequest` 的 `RunResult` 外部合同，并支持 `stagnant_turns` 和 provider failure 传播；
- `research_iteration` 需要研究 ledger、续作上下文和研究权限；
- `project_audit_advisor` 使用独立 executors、deadline 和隔离的只读 durable run。

所以目前看到的是**重复装配代码**，不是重复的业务循环。可以把公共字段做成一个 typed builder，但 builder 必须允许四个入口明确填入各自的边界字段；如果 builder 只是把差异藏进 `getattr` 或 `Any`，复杂度会从调用点搬到更难审计的地方。

### 当前判断

入口装配统一是后续可做的重构候选，暂不在 K-P1 中混入。先用留出任务验证现有精简对 Research、Project 只读、Project 写入、恢复和不同协议是否保持边界；如果这些行为稳定，再单独为 builder 建立结构实验。

## 阶段 7：K-P1 留出任务 A/B（2026-11-05）

为了检查 K-P1 是否只在原先那批题目上有效，使用新的留出任务运行原版 A 和实验树 B。两边使用相同的 KoboldCpp、模型、temperature、protocol、seed 和案例顺序：`search_quoted`、`search_regex`、`search_empty`、`cancel`、`search_cancel`，覆盖 seed 76、77 的 native/on 与 text/off，共 20 个案例/树。机器对照产物是：

```text
.e2e-artifacts/kernel-audit-20261104/holdout-ab-comparison-20261105.json
```

### 结果

| 指标 | 原版 A | K-P1 B |
|---|---:|---:|
| 案例数 | 20 | 20 |
| `ok` | 18 | 17 |
| 模型请求数 | 43 | 38 |

这次留出脚本没有记录 prompt token，所以不能用它估算输入 token 收益；之前的 K-P1 配对产物仍然是 token 证据来源。

失败不是固定地落在实验树：

- A 的两个 seed 都在 native/on 的 `search_empty` 遇到同一个 `done arg 'summary' must be string (got dict)`；
- B 的 seed 76 native/on `search_empty` 反而通过；
- B 的 seed 77 出现一次 native `provider returned malformed tool calls`、一次 text `invalid JSON`，而 A 的对应案例通过；
- 取消案例两边都保持 `stop_reason=stopped`、`repeat_recovery_stable=true`。

失败位置在 A/B 之间移动，且同一模型、同一 seed 的请求轮数也明显变化。这说明当前本地模型服务存在响应和协议输出波动；它不能证明 K-P1 让模型变好，也不能证明 K-P1 造成回退。B 少 5 次请求是观察结果，不能在这组不稳定的案例上宣称收益。

### 失败根因判断

`search_empty` 的 grep/codemode 执行本身成功，错误发生在模型生成的 `done` 参数类型；`malformed tool calls` 和 `invalid JSON` 也发生在 provider 返回的协议文本，且 `wire_contract_consistent` 仍为真。它们属于模型/provider 输出波动，不是任务投影、durable、恢复或工具执行代码抛出的生产错误。当前不修改生产协议来迁就这些单次输出；应在更多留出题和重复运行中观察是否有稳定差异。

## 阶段 8：审计回归与测试夹具修正（2026-11-05）

留出任务之后运行了入口、capability、durable/runtime effect、恢复、completion 和 K-P1 prompt 相关的定向扩大回归，没有运行全量 pytest：第一次选择集为 **173 passed、2 skipped、13 个子断言通过**，唯一失败是 `tests/test_manual_project_adapter_entry_contract.py` 把 `tests/manual/kernel_retry_live.py` 内部的低层 `run(...)` helper 误判成 Project adapter 入口。

检查生产代码后确认这是测试扫描器问题：该脚本直接调用 `run_task_kernel`，并没有导入或调用 `codey.operations.project_adapter.run`。扫描器原来只排除模块级函数名，没有识别嵌套的本地 `run` helper。已在测试夹具中改为收集 AST 中所有本地函数名，再判断真正的 adapter 入口；没有放宽对外部 `run(AgentRequest)` 调用的检查。

修正后的相关回归子集：**85 passed、2 skipped、7 个子断言通过**；重新运行原来的完整选择集后为 **174 passed、2 skipped、13 个子断言通过**。质量检查结果：

- Ruff：通过；
- Mypy：`codey` 575 个源文件通过；
- Pyrefly：0 errors，既有 35 suppressed、79 warnings；
- ty：`ty check codey` 通过。仓库级 `ty check` 仍会报告既有 tests/tools 类型诊断，因此不把它伪装成全仓通过；
- `git diff --check`：通过。

当前结论仍是：K-P1 保留在实验树，暂不合并 master；capability snapshot 和入口装配只形成后续 typed builder/view 候选，没有新的可安全删除字段。

## 阶段 9：K-P1 扩大验证计划（2026-11-05）

下一轮工作先围绕已经改动过的 K-P1，不能同时改 capability 或入口装配。目标是回答一个简单问题：省掉初始观察块里的重复任务文字后，Codey 在不同任务、不同协议和恢复中是否仍然可靠。

### 验证矩阵

| 维度 | 最小覆盖 |
|---|---|
| 任务 | 读文件、搜索、修改、验证、Research、Project、Ghost |
| 协议/provider | native、text，以及环境中可用的其他 provider |
| 重复运行 | 同一任务使用多个 seed，并重复运行，避免把一次模型随机回答当成结论 |
| 成本与行为 | 完成率、协议错误、请求轮数、输入/输出 token、工具调用、最终文件和答案 |
| 恢复 | 请求前、工具后、effect intent 后、settlement 前、completion 前、进程重启后 |

### 恢复验收

每个中断点都必须确认：任务原文仍在；恢复不会重复执行不可重放 effect；工具调用和 call identity 不重复；文件、durable revision 和 checkpoint 不损坏；completion 不会绕过验证；重复恢复结果稳定。任何一项失败，都先判断是测试夹具落后还是生产边界错误，再决定修测试或撤回候选。

### 合并门槛

K-P1 只有同时满足以下条件才可考虑合并：不同任务类别完成率不下降；不同协议/provider 的工具合同和权限边界不变；重启/恢复没有回退；输入 token、请求轮数或准确率至少有跨 seed 的稳定收益；结果不能只依赖单个模型、单个 seed 或一次实机运行。若只有 token 变少但行为证据不足，继续留在实验树。

### 后续候选隔离

K-P1 得出结论后，才分别开启两个独立实验：

1. capability snapshot → typed capability view；保留 durable snapshot、`window_ref` 和注入 provider 兼容入口；
2. 四类入口重复装配 → typed `KernelRunRequest` builder；保留各入口的授权、恢复、隔离和外部 API 差异。

两个实验不能和 K-P1 同时修改，以便知道任何行为变化来自哪一个候选。

## 阶段 10：共享消费者与恢复确定性扩大回归（2026-11-05）

先运行不依赖模型随机性的扩大选择集，覆盖 Research whole/continuation/completion、Project adapter 与 advisor、Ghost continuity/context/provider boundary、kernel recovery、durable effect replay、checkpoint 和 completion fail-closed。结果为 **214 passed、2 个子断言通过**，没有生产代码失败，也没有运行全量 pytest。

这一步只说明 K-P1 当前改动没有破坏共享消费者和恢复边界；它不能替代不同 provider、不同任务和真实模型的 A/B，因此下一步仍需运行实机矩阵。

## 阶段 11：K-P1 新实机任务矩阵（2026-11-05）

使用 KoboldCpp 同一模型、temperature 0.3、native/text 两种协议、seed 78/79，覆盖读文件、创建文件、替换文件、字面搜索、上下文搜索、取消搜索和取消任务，共 28 个案例/树。原版 A 和实验版 B 的原始请求、响应、usage 都保存在机器产物中：

```text
.e2e-artifacts/kernel-audit-20261104/holdout-expanded-ab-20261105.json
```

### 行为结果

| 指标 | 原版 A | K-P1 B |
|---|---:|---:|
| 案例 | 28 | 28 |
| `ok` | 28 | 27 |
| 模型请求 | 56 | 62 |
| 首轮 prompt token | 103,520 | 101,436 |
| 全部 prompt token | 223,582 | 242,777 |

K-P1 的首轮 prompt token 少 **2,084（约 2.0%）**，说明重复任务文字确实被省掉；但因为 B 在 `seed 78 / text / search_cancel` 遇到 provider 返回的 invalid JSON，且请求轮数增加，全部 prompt token 反而多 **19,195（约 8.6%）**。该失败没有执行任何工具，属于模型/provider 协议输出问题，不能直接归因于 K-P1；但也不能把 B 的成本下降宣称为稳定收益。

### 审批、冷 provider 和恢复

`shell_approval_task_identity_live.py` 在原版 A 与实验版 B 各跑 warm/cold 两次，共 4 个实机场景；机器产物为：

```text
.e2e-artifacts/kernel-audit-20261104/holdout-shell-approval-ab-20261105.json
```

四次都通过：每次 2 个模型请求、只执行 1 次 shell effect，原任务保留、tool call identity 配对、冷 provider 继续运行、最终 `stop_reason=done`。这证明 K-P1 的恢复边界在这个真实审批场景没有回退。

### 测试夹具根因

第一次运行实验树审批脚本时，夹具要求连续请求的观察块必须没有 `user_task`；实际设计是初始外层 prompt 省掉重复字段，而审批/恢复请求仍可以保留任务锚点。生产代码按设计工作，错误在测试断言过严。修正为只要求原任务仍然存在，不强行规定连续观察块的内部投影形式后，相关测试 **10 passed**，实验树 warm/cold 实机均通过。

### 当前判断

这批任务确认 K-P1 有稳定的首轮输入缩减，也确认审批/冷 provider 恢复没有回退；但总请求数和总 prompt token 仍受模型协议随机性影响，B 还有 1 个 provider 输出失败。因此 K-P1 继续留在实验树，暂不合并。下一步是把不同 provider/Research/Ghost 的可用实机或确定性等价场景补齐，并增加故障点中断恢复矩阵。

## 阶段 12：K-P1 收口决定（2026-11-05）

K-P1 到此停止继续扩展测试，作为一个完整候选写入审计结论：

- 它确实删除了初始请求中重复的任务文字，多个 A/B 产物都显示首轮 prompt 变短；
- 它没有证明整个任务更省：新矩阵中首轮 prompt token 下降约 2%，但实验版总请求数从 56 增至 62、全部 prompt token 从 223,582 增至 242,777；
- 实机失败属于模型/provider 协议波动，不能证明 K-P1 本身造成回退，但也不能把首轮节省包装成稳定产品收益；
- 恢复、审批、冷 provider、工具合同和确定性共享消费者回归没有发现边界回退。

最终处理：**K-P1 保留在实验树，不合并 master，也不再继续投入验证预算**。后续精力转向结构候选：capability typed view 和入口 `KernelRunRequest` builder；两者各自独立实验，不能与 K-P1 混改。

## 阶段 13：capability typed view 结构实验（2026-11-05）

第一个独立结构实验已完成：新增 `codey/operations/kernel_capabilities.py::KernelCapabilityView`，由 kernel 入口一次性读取 provider 实例的运行时事实：

- `native_tools`；
- `text_tool_format`；
- `request_scoped_tool_context`；
- `window_ref`，没有 provider window 时沿用 transport 的冻结 window。

`KernelContext` 后续只使用这个 typed view，不在多个决策点重复 `getattr`。静态 `ProviderCapability`、durable `ApiRunSelection`、snapshot 字段和 provider 对外属性都保留；这个实验只整理读取方式，不删除任何兼容字段，也不改变协议选择。

### 验证

- capability view、provider protocol、K-P1 相关 kernel 选择集：**8 passed**；
- 另一组 provider/context/window 回归：**31 passed、7 个子断言通过**；
- Ruff：通过；
- Mypy：相关 2 个源文件通过。

一次初始实现让测试 monkeypatch 失效，因为 view 直接导入了函数副本；根因是测试接线而非生产协议，已改为通过 `kernel_transport` 模块动态读取，之后回归通过。

### 当前判断

这个 view 减少了 kernel 内部重复能力探测，但它本身没有模型收益，也没有删除 durable/provider 合同。先保留为独立实验，待入口 builder 完成后再一起做结构回归；两个实验仍保持可分别回滚。

## 阶段 14：入口 `KernelRunRequest` builder 结构实验（2026-11-05）

第二个独立结构实验已完成：新增 `codey/operations/kernel_request_builder.py::KernelRunRequestBuilder`，并将四个生产入口的重复 transport 装配改为通过 builder 生成 `KernelRunRequest`：

- `task_entry`；
- `project_adapter`；
- `research_iteration`；
- `project_audit_advisor`。

builder 只负责重复的 transport 字段：provider、run id、effect scope、provider id、原始任务、上下文、Ghost 来源、attempt gate、停止状态和窗口引用。四个入口的 `KernelExecutionDeps` 与 `KernelObservationDeps` 仍在调用点显式传入，因此授权、工具、workspace writer、Research ledger、advisor 隔离、durable boundary 和外部 API 差异没有被藏进 `Any` 或动态分支。

### 验证

编译、Ruff、Mypy、Pyrefly 和 ty 的相关检查通过。入口、任务 kernel、Project、Research、advisor、恢复和依赖边界定向回归为 **99 passed、6 个子断言通过**，没有运行全量 pytest。

这次没有重新跑 K-P1 实机矩阵；builder 是装配结构整理，实验目标是证明行为合同保持不变。现有回归覆盖四个入口仍调用同一个 `run_task_kernel`，typed request 的字段和依赖边界保持成立。

### 当前判断

这个 builder 确实减少了四处重复的 `KernelTransportDeps` 文字，同时保留了入口之间必须可审计的执行和观察差异。它没有删除 durable/provider 字段，也没有直接带来模型 token 或轮数收益；收益是降低以后修改 transport 装配时漏改一处的风险，并让共享 request 的边界更集中。

因此先保留在实验树，作为可单独回滚的结构改动；不把它和 K-P1 合并，也不据此宣称模型性能提升。后续若要合并 master，需要在最终候选组合上再做一次共享消费者、恢复和静态检查回归。

## 阶段 15：移除 K-P1，重置审计基线（2026-11-05）

K-P1 的实验代码已经从审计分支移除。审计分支不再沿用 K-P1 的提交链，而是重置到当前主树 `bf91046a`，其中保留已经合入主树的两个结构整理：

- `KernelCapabilityView`；
- `KernelRunRequestBuilder`。

K-P1 的实机数据、失败根因和“不合并”结论保留在本报告的阶段 11、12 以及 `.e2e-artifacts/kernel-audit-20261104/` 产物中。当前 `codey/` 和 `tests/` 中已没有 K-P1 的 `include_task` 投影改动。这样实验树现在代表真实生产代码的审计基线，而不是一个已经否决的模型提示变体。

## 阶段 16：`recovery_summary` 残余分支审计（2026-11-05）

静态读取 `codey/runtime/effects/effect_records.py`、`codey/runs/details.py` 和全部生产调用者后，`RecoverySummary.interrupted_repairs` 的事实如下：

- 类型字段有默认值 `0`；
- `RuntimeEffectStore.recovery_summary()` 只在局部初始化为 `0`，没有任何生产分支把它增加到非零；
- `runs/details.py` 只读取它来判断是否存在 recovery 信息，并在复制 summary 时原样转发；
- 当前 recovery 行为只会产生 interrupted write、unconfirmed provider call、retryable read-only、replayed read 和 replayed lookup 五类事实。

所以这是一个真实的残余分支，但还不能直接删除：`RecoverySummary` 是可导入的运行时类型，外部调用者可能手工构造该字段；它也没有独立的持久化版本迁移。当前处理为 **保留字段，标记为兼容性疑似死分支**。若未来要删，必须先确定外部 API 兼容策略，并增加旧对象反序列化/构造验证。

确定性验证：runtime effect records、recovery summary、hardening 和恢复相关选择集通过；本轮组合回归中相关断言也覆盖了 provider 未确认、只读重放和中断写入。

## 阶段 17：completion gate 重复计算审计（2026-11-05）

沿着 `kernel_driver → kernel_control.evaluate_completion → operations.completion_gate.evaluate` 追踪后，当前最终完成证明只有一个组合入口：

- `project_completion_checks`、`research_completion_checks` 和注册 provider 只产生 `CompletionCheck` 行；
- `operations.completion_gate` 负责去重、构造 contract、生成最终 `CompletionProof`；
- `kernel_control` 只消费一次 verdict，并把 accepted proof 写入 kernel state；
- `DurableSession.complete_task/finish_task` 只负责 completion pending、子任务 join 和终态提交，不重新计算 proof；
- `TaskRuntime._settle_if_open` 是 provider/执行器提前返回时的终态兜底，不是第二个 completion gate；
- `research.completion_gate.ResearchCompletionGate` 处理的是 Research 队列发布结果的外层投影，输入和边界不同，不是共享 kernel gate 的重复实现。

因此目前没有可安全删除的“第二次完成判定”。表面上存在多个 completion 名称，实际分别属于检查行生产、kernel 最终证明、durable 生命周期提交和 Research 发布投影。当前结论为 **保留现状，不做合并或删除**。

确定性验证：completion ownership、selected verification、proof authority、kernel completion、Research completion 和 durable completion 选择集通过；其中已有断言确认共享 kernel 对 `completion_gate.evaluate` 只调用一次。

## 阶段 18：高扇入模块与导入边界审计（2026-11-05）

对当前 718 个 `codey` Python 模块做 AST 导入盘点，最高扇入模块如下：

| 模块 | 生产导入者数 | 当前判断 |
|---|---:|---|
| `codey.utils.refs` | 92 | 稳定的小型身份/引用工具，不能按扇入直接拆分 |
| `codey.durable.model` | 57 | 持久模型和状态解析的共享事实层，拆分风险高 |
| `codey.storage.local_store` | 56 | 存储底座，多个 app/durable/runtime 消费者确实需要 |
| `codey.runtime.core.models` | 48 | 运行时公共值对象，需按类型所有权专项拆分 |
| `codey.toolchain.tool_spec` | 43 | 工具合同和 schema 事实源，不能复制到各 provider |
| `codey.providers.base` | 33 | provider 公共协议类型和基础行为，暂未发现重复实现 |
| `codey.knowledge.model` | 33 | Knowledge 持久对象模型，扇入来自真实读写消费者 |
| `codey.providers.diagnostics` | 32 | 错误/诊断规范化共享层 |
| `codey.runtime.core.operation_payload` | 30 | durable operation payload 解析和序列化共享层 |
| `codey.runtime.effects.effect_records` | 27 | effect intent/settlement/recovery 事实源 |

导入图发现 13 个强连通分量，其中较大的分量包括：Research answer/tool contract 与 toolchain schema、kernel 编排模块、durable checkpoint 与 runtime delivery。进一步追踪后发现：

- kernel 分量主要由函数级延迟导入和共享 driver/context/task loop 互相协作形成；
- durable checkpoint ↔ runtime delivery 通过恢复投影互相读取事实；
- Research 分量把答案合同、证据 ledger 和工具 schema 绑定在同一业务闭环；
- 已有架构测试继续禁止 runtime 反向依赖 app/agents/operations、agents 依赖 operations、provider wrapper 依赖 kernel transport，以及 durable kernel 导入 host layers。

因此“高扇入”与“导入成环”目前只证明组织复杂度，不能证明某个模块无用。当前没有足够证据做大范围拆分；后续若继续精简，应先挑一个强连通分量，冻结公共类型和导入契约，再做单独实验。

## 阶段 19：审批执行前置检查修正（2026-11-05）

本轮专项回归发现 `execute_approved_shell()` 在审批代次和停止标记确认前调用 `resolve_shell()`；后者会启动短暂 shell 探测进程。审批代次读取失败或任务已停止时，虽然真正命令不会执行，但探测进程已经产生，违反 fail-closed 的执行边界。

根因属于生产代码的顺序问题，不是测试夹具问题。审计分支已将审批代次和 stop flag 检查前移到 shell discovery 之前，并保留 `execute_shell_ticket()` 内的二次检查以防并发变化。修正后的专项回归为 **82 passed、5 skipped**；与 recovery、completion、依赖边界、架构和 source connector 选择集合并回归为 **243 passed、2 skipped、456 个子断言通过**。Ruff、Mypy、Pyrefly、ty 和编译检查通过。

当前处理：该修正随后以独立提交 `05e47aa8` 合入主分支。它与 K-P1 无关；主树原有的未提交 Zen 改动未被覆盖。

## 阶段 20：当前审计结论（2026-11-05）

第一轮内核审计已经覆盖主要 operations、runtime、durable、providers、app 及 Ghost/Research/Project 消费者。K-P1 已从实验代码移除，只保留证据和否决结论；两个结构整理已经成为新的审计基线。

本轮新增专项没有发现可以直接删除的 completion gate、high fan-in 模块或导入边界。唯一确认的残余是 `interrupted_repairs` 兼容字段，以及审批执行前置检查这一项生产顺序问题。前者先保留，后者已单独修正并进入主树。

## 阶段 21：审批前置检查进入主树（2026-11-05）

审批执行顺序修正已经在主树复核并合入：`05e47aa8 fix(shell): validate approval before discovery`。主树定向回归为 **101 passed、7 skipped**；Ruff、Mypy、Pyrefly、ty 和编译检查通过。

审计分支继续保留完整的专项报告和修复证据；K-P1 仍然只存在于报告和历史产物中，没有重新进入任何代码树。

## 阶段 22：删除 `interrupted_repairs` 残余接口（2026-11-05）

根据阶段 16 的生产调用者盘点，直接删除 `RecoverySummary.interrupted_repairs`，没有增加兼容壳：

- 从 `RecoverySummary` 删除字段；
- 删除 `RuntimeEffectStore.recovery_summary()` 的局部变量和返回参数；
- 删除 `runs/details.py` 的存在性判断和原样转发；
- 重新搜索生产代码、测试、文档和序列化路径，没有残留引用；
- 该类型没有持久化 payload，也没有仓库内外部构造合同的证据。

主树提交：`80ab0e21 cleanup(recovery): remove unused repair summary field`。相关 effect records、delivery、run details、durable recovery 和 hardening 选择集为 **60 passed、2 skipped**。这是一次纯运行时接口清理，没有模型输入变化。

原先“因为类型可导入所以暂不删除”的判断已作废；可导入性是兼容性风险提示，不是保留理由。

## 阶段 23：kernel provider request 回边整理（2026-11-05）

将 `KernelContext.freeze()` 反向导入 `kernel_driver.prepare_provider_request()` 改为依赖新的 `codey.operations.provider_request`：

- `kernel_driver.prepare_provider_request` 仍然保留为兼容导出，现有测试和调用者不变；
- provider request payload、effect identity、durable mutation 和恢复格式完全保持原样；
- `kernel_driver` 继续作为动作驱动器，provider request 冻结职责单独归位；
- prepared request、not-sent continuation、kernel recovery、dependency direction、architecture 和 durable boundary 选择集为 **169 passed、450 个子断言通过**；
- Ruff、Mypy、Pyrefly、ty 和编译检查通过。

主树提交：`ec6a63f0 refactor(kernel): isolate provider request preparation`。

AST 导入图的强连通分量总数仍为 13，因为 kernel 还存在 `context ↔ control` 以及 Research continuation/task loop 的真实协作边。这个提交只清除一个明确的反向动态依赖，没有用搬 import 的方式假装整体循环消失。

## 阶段 24：durable / delivery 循环处理决定（2026-11-05）

`durable.checkpoint ↔ runtime.effects.tool_result_delivery ↔ runtime.log.session_view` 仍然存在函数级依赖。当前没有把它们强行拆开，原因是：

- `checkpoint_entries()` 是唯一的 durable facts → runtime entries 投影；
- delivery Store 通过这条投影读取恢复事实；
- `session_view` 同时负责 operation、effect 和 delivery 三类运行时视图；
- 只移动一个 envelope helper 会留下间接环，或者迫使 delivery 重新实现 checkpoint/recovery 校验。

因此当前只完成 kernel provider request 的小步边界整理，durable/delivery 留作下一次独立实验。后续若要继续，必须先把纯 delivery contract、durable adapter 和 SessionView 的所有权画清楚，再以不复制校验逻辑为前提拆分。

## 阶段 25：审计报告归档到主树（2026-11-05）

完整报告已迁移到主树：

```text
docs/reports/kernel-carpet-audit-20261104.zh-CN.md
```

报告依赖的 `.e2e-artifacts/kernel-audit-20261104/` 证据目录需要与主树一起保留。后续内核清理只在 `D:\codey` `master` 上进行，审计 worktree 和 `audit/kernel-audit-20261104` 分支在报告提交后删除。

