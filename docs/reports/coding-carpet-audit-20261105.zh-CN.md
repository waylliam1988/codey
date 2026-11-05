# Codey 编码地毯式精简审计（2026-11-05）

## 目的

沿用 `docs/reports/kernel-carpet-audit-20261104.zh-CN.md` 与
`docs/research-complete-branch-review-20261103.zh-CN.md` 的证据纪律，审计 Codey
的完整编码链路：

```text
用户编码需求
  → Chat 临时工作区或 Project 入口
  → 路由、策略和工作区上下文
  → 自动规划 / 前置项目审计（按条件）
  → Writer 读取、编辑、运行验证
  → Review / 返修（按条件）
  → completion gate、编辑完整性和持久收据
  → diff、结果、恢复和项目记忆
```

本审计允许把自动规划、前置项目审计、自动 Review、返修、项目地图、编码上下文和
相关 prompt 本身列为删除或简化候选。权限、真实工具回执、并发写入、恢复、幂等、
编辑完整性和完成证明先按独立系统边界验证，不能因为模型看不见就删除。

## 已对齐的判断标准

- 覆盖完整编码链路，同时覆盖 Chat 临时工作区与关联 Project 入口。
- 先保护实际编码质量，再比较代码复杂度、模型请求轮数、输入/输出 token、耗时和无效调用。
- 先使用当前 KoboldCpp 本地模型；关键候选再做跨模型复核。
- 实机任务覆盖：已有项目修 bug、增加功能、跨文件重构、从零生成项目。
- 不运行全量 `pytest`。测试按生命周期、权限、恢复、Review、验证和候选逐组运行。

## 证据等级

| 等级 | 证据 | 能说明什么 |
|---|---|---|
| E0 | 文件名、行数和命名 | 只能提出候选 |
| E1 | 静态调用、入口和消费者追踪 | 说明实际路径和边界候选 |
| E2 | 定向合同、故障注入、恢复和权限回归 | 说明程序不变量 |
| E3 | 固定模型、材料、seed 的真实 A/B | 说明模型请求和任务表现差异 |
| E4 | 新 seed、留出题、内容核查和跨模型复核 | 支持是否进入主线 |

## 实验隔离与基线

- 主树：`D:\codey`，分支 `master`。
- 本报告早期曾使用临时工作树 `D:\codey-coding-audit` 和分支
  `audit/coding-audit-20261105` 做隔离实验；改进已逐项合并回主树，临时工作树和分支现已删除。
- 起始提交：`6ab91052`。
- 主树保留一个与本审计无关的未跟踪文档 `docs/cline-integration-implementation-plan.zh-CN.md`，本审计没有修改它。
- 旧报告记录的 `D:\codey-kernel-audit` 已不存在。
- 当前阶段：已完成第一轮候选实机，发现并修复两个确定性边界问题；继续扩大完整编码链路样本。

## 阶段 0：静态盘点（2026-11-05）

### 范围

按编码直接相关的 `operations`、`agents`、`completion`、`workspace`、`reviews`、
`providers`、`codemode` 包做 AST 盘点。盘点包含这些包的所有 Python 模块，因此
也包含 provider 和共享边界候选；它不是“编码每次运行都会调用”的证明。

### 结果

- 220 个模块；55,277 行；2,575 个类/函数定义。
- 高复杂度候选：`workspace/changes.py` 1,415 行、`providers/controls.py` 1,384 行、
  `providers/worker.py` 1,019 行、`providers/api_provider.py` 988 行、
  `workspace/map.py` 894 行、`completion/edit_integrity.py` 875 行、
  `completion/verification_policy.py` 863 行、`operations/task_execution.py` 805 行、
  `operations/task_run.py` 803 行、`operations/task_entry.py` 749 行、
  `operations/kernel_protocol.py` 738 行、`operations/kernel_execution.py` 682 行、
  `reviews/core.py` 665 行、`operations/project_completion_context.py` 603 行、
  `operations/kernel_context.py` 599 行。
- 这些数字只用于确定审计优先级，不构成删除结论。

### 当前主要生产入口

| 入口 | 位置 | 作用 |
|---|---|---|
| Chat / auto / hybrid 任务 | `operations/task_entry.py`、`operations/task_phases/dispatch.py` | 建立策略、工作区、工具快照并进入共享 `run_task_kernel` |
| Project Writer | `operations/project_completion_flow.py` | 构建项目上下文，执行 Writer，进入候选验证、Review 和完成执行 |
| Project adapter | `operations/project_adapter.py` | 保留 `AgentRequest` / `RunResult` 合同，调用共享 kernel |
| Project audit advisor | `operations/project_audit_advisor.py` | 对已有项目提供前置审计建议，再回到 Writer 任务 |
| Consensus / planning | `agents/consensus.py`、`app/consensus_service.py` | 新项目计划或 Writer 后咨询；结果是建议，不直接写文件或批准完成 |
| Review | `app/review_service.py`、`operations/project_review_phase.py`、`reviews/*` | 对当前变更做只读审查，必要时有限返修 |
| 完成与恢复 | `completion/*`、`operations/project_completion_enforcement.py`、durable/runtime | 验证、编辑完整性、完成证明、持久收据、恢复和幂等 |

## 阶段 1：初步生命周期与候选（2026-11-05）

### 生命周期

```text
TaskSubmission
  → dispatch / task policy / durable recovery
  → Chat: task_entry → run_task_kernel
  → Project: ProjectTaskContextBuilder
       → 新项目 consensus plan（无用户文件）
       → 已有项目 project audit advisor（有用户文件）
       → project_adapter / project_writer_phase
  → project map + instructions + verified facts + checkpoint + repair context
  → Writer kernel：read / edit / codemode / run / done
  → candidate validation
  → ReviewCoordinator / Reviewer
       → approved，或一次有界 Writer repair，再 review
  → completion engine + completion gate + edit integrity
  → durable result / receipt / diff / project facts / memory
```

### 候选分组

| 编号 | 候选 | 初步问题 | 证据要求 |
|---|---|---|---|
| C-P1 | 新项目自动 consensus 规划 | 规划是否减少错误和返工，还是增加一次模型请求和锚点漂移 | E3/E4，需从零生成任务 |
| C-P2 | 已有项目前置 audit advisor | 审计建议是否改变最终修改质量，还是与 Writer/Review 重复 | E3/E4，需已有项目 bug/功能任务 |
| C-P3 | Writer 后 consensus 咨询 | 对未修改或只回答的结果是否有收益，是否会改写正确答案 | E3/E4，需保留 draft/最终内容对照 |
| C-P4 | 自动 Review | Reviewer 是否发现 Writer/测试未发现的真实问题，额外成本是否值得 | E2 + E3/E4，需内容核查和独立文件检查 |
| C-P5 | Review 触发的 Writer repair | 返修是否修复真实问题，是否引入新改动或重复工作 | E2 + E3，需失败/修复留出题 |
| C-P6 | project map / verified facts / checkpoint 投影 | 模型是否因这些上下文少读文件、少改错文件，或被陈旧信息干扰 | wire、重复读取、错误编辑、token |
| C-P7 | coding current context | 当前已读/可编辑/变更/验证状态是否减少重复动作和遗漏验证 | 已有 A/B 可复核，并扩展四类任务 |
| C-P8 | completion repair prompt 与协议修复 | 失败后修复是否真正恢复协议，是否只是增加轮次和长提示 | 已有 repair A/B + 真实编码失败任务 |
| C-P9 | codemode 与原生/文本工具合同 | 嵌套工具接口是否带来稳定收益，还是增加编译/协议失败 | native/text、多 seed、协议错误记录 |
| C-P10 | 项目记忆与 verified facts | 成功后保存的事实是否帮助后续任务，还是污染上下文/增加负担 | 冷启动、续作、错误事实回归 |
| C-P11 | Review impact map | Review 是否需要程序预计算的调用影响提示，还是只增加 prompt | Review 内容 A/B、真实引用命中和误报 |

### 当前判断

1. 目前不能因为模块大、入口多或存在第二模型就判定可以删除。
2. C-P1～C-P5 是“业务流程是否值得存在”的高价值候选；C-P6～C-P9 是“模型输入和工具协议是否过重”的候选。
3. completion、编辑完整性、权限、durable 和恢复先作为安全底座；只有证明重复职责且 E2 不退步后才考虑结构合并。
4. 第一批实验应先做可隔离的上下文/规划变量，再做 Review/返修，因为后者必须先定义真实缺陷和内容核查标准。

### 阶段 0 定向基线结果

- 编码相关定向回归：**402 passed、33 个子断言通过、0 failed**，耗时 70.96 秒。
- 覆盖编码上下文、Project 入口、项目地图、完成证明、编辑完整性、验证候选、Review 生命周期、Review 返修、Consensus、adapter 恢复和共享 kernel 边界。
- Ruff：目标编码包通过。
- Mypy：目标编码包 145 个源文件通过。
- 尚未运行全量 `pytest`，也尚未运行真实模型 A/B。
- 当前没有生产代码失败；后续若实机 harness 或测试出现失败，将记录具体根因并区分夹具、接线和生产逻辑。

## 后续计划

1. 冻结并运行编码相关定向 E2 回归，记录首次失败根因，区分测试夹具问题与生产问题。
2. 读取现有 coding A/B harness，确认 KoboldCpp endpoint、模型、seed、输出和工作区隔离方式。
3. 为四类任务建立固定材料和成功判定：最终文件、diff 范围、测试/行为、任务要求、请求轮数和 token。
4. 先运行 C-P6/C-P7 的生产前基线与 A/B，再逐个审计规划、前置 audit、Review 和返修。
5. 每个候选独立提交；有真实生产问题先修复并补测试、Ruff、Mypy/Pyrefly/ty，再更新本报告。

## 阶段 2：编码上下文首轮实机与 Windows 验证根因（2026-11-05）

### C-P7 首轮实机

使用 `tests/manual/coding_current_context_ab.py`、本地 KoboldCpp 和临时项目，先比较
`coding_context_enabled=False/True`。首轮单例 `edit-then-verify` 中 baseline 完成
1/1，context 发生两次空回复后停在 protocol；随后 context 单独重跑完成 1/1。
这不能归因于上下文，因为 baseline 也在另一轮出现空回复，且 KoboldCpp 生成存在自然波动。

扩大到三个固定题后，分别运行两个 arm：

- `avoid-duplicate-read`：baseline 1/1，context 1/1；两者都只改目标文件并通过 pytest。
- `edit-then-verify`：两轮中分别出现空回复导致 protocol stop；无工具调用，不能作为编码质量差异。
- `monorepo-selected-check`：模型成功修改前端文件，但独立检查最初报告 `npm` 找不到；随后确认生产 `run` 也在 Windows 上无法用裸 `npm` 启动，见下节。

成功样本的输入字符数为 baseline 94,344、context 113,872；context 发送了 3 个当前上下文投影，baseline 为 0。样本太少且请求结果有波动，当前不能宣称 C-P7 有模型收益；它至少说明当前上下文投影会增加输入，必须以最终质量、重复动作和验证遗漏共同判断。

### 确定性生产 bug：Windows `.cmd` shim

`monorepo-selected-check` 的验证命令来自项目 `package.json`：`npm test`。在本机：

```text
shutil.which("npm") → C:\Program Files\nodejs\npm.CMD
subprocess.run(["npm", "--version"], shell=False) → FileNotFoundError [WinError 2]
```

生产 `codey/toolchain/runtime.py::run_command_raw` 的策略检查使用 `shutil.which`，
但随后把裸 argv 直接交给 Windows `CreateProcess`。这使候选检查被判定可用，却在真正执行时失败。

处理：

- 主工作树修复提交：`02d8c2fd fix(toolchain): resolve Windows command shims`。
- 实验树同步提交：`2c54adf7`。
- 新增确定性回归：Windows 模拟环境中 `npm test` 启动参数解析为 `npm.CMD`。
- 不改变 canonical 用户命令、权限策略、白名单、shell 审批或审计显示。
- 修复后真实临时项目 `npm test` 返回 exit 0，并输出 `NPM_OK`。
- 主树定向回归：162 passed、23 个子断言通过；生产文件 Mypy 通过；Ruff 通过。

原有手工 harness 的独立检查也使用裸 `subprocess.run(["npm", ...])`，因此在第一次
修复后仍把成功的 Codey 工具回执记成失败。这是测试入口接线问题，已在审计期间的
`tests/manual/coding_current_context_ab.py` 中改为先用 `shutil.which` 的解析路径启动，
并通过编码上下文/运行时定向测试。它不改变生产代码。

### 当前判断

1. Windows 命令解析 bug 是已确认的生产问题，已修复并进入主树；此前 `npm` 失败不能算模型或上下文退步。
2. C-P7 当前没有足够 E3 证据合并或删除；成功样本显示输入变长，空回复在两个 arm 都出现，先暂缓结论。
3. 继续实机前，验证 harness 必须使用与生产相同的命令解析和独立检查语义；模型失败、工具失败、独立检查失败要分栏记录。
4. 下一批优先运行四类任务中不依赖外部包管理器的 bug、功能、跨文件重构和从零生成题，再扩大 seed；之后审计 C-P1/C-P2 的规划与前置 audit。

## 阶段 3：Review 上下文 A/B（2026-11-05）

### 静态触发核对

- 新项目无用户文件时，`project_completion_flow._prepare_new_project_context` 才会调用 consensus 的 `plan=True, draft_first=True`；它可能包含 owner draft、advisor 和 aggregate 请求，结果写入 `change_brief` 后才进入 Writer。
- 已有项目有用户文件时，前置 `run_project_audit` 才有机会运行；它为每个可用 advisor 开启只读 shared-kernel 子任务，只有非空报告才改变 Writer task。
- Writer 后 consensus 只在 `done` 且没有发生文件修改、且没有前置 audit 时运行，因此正常编码修改不会同时得到这个咨询分支。
- Project `validate_candidate` 会先做候选检查，再进入 ReviewCoordinator；只有 `done`、确有变更、变更可审查且没有停止请求时才发送 Reviewer。发现 `needs_writer_repair` 后最多一次 Writer repair，并在修复完成后重新 Review。
- Review approved 不替代本地验证和 completion proof；Review unavailable/unknown 也不会盲目重发或把结果写成通过。

这些条件说明自动规划、前置 audit、Review 和返修不是每个编码任务都发生，但它们确实会增加模型请求和状态转换；后续应按触发条件做候选对照。

### C-P11 Review impact map 实机

使用生产 `render_review_prompt`、`render_review_impact_map`、本地 KoboldCpp 和三个固定 Review fixture，比较无 impact map 的 `current` 与带 bounded impact map 的 `impact_map`。这是 Review 模型输入 A/B，不改变 Writer、权限或 Review 解析器。

主树当前 Review 入口在未显式传入 impact map 时会调用 `safe_review_impact_map` 自动生成，因此这项能力已经存在于默认 Project Review 链路；本次决定是确认保留该默认行为。

| 指标 | current | impact_map | 差异 |
|---|---:|---:|---:|
| 题数 | 3 | 3 | 0 |
| 正确 verdict | 3/3 | 3/3 | 0 |
| issue 命中 | 3/3 | 3/3 | 0 |
| 影响引用命中 | 0/2 适用题 | 2/2 适用题 | +2 |
| 相关测试命中 | 1/1 适用题 | 1/1 适用题 | 0 |
| 平均质量分 | 5.000 | 6.333 | +1.333 |
| 平均 prompt 字符 | 3,169.7 | 4,006.3 | +836.6 |
| 误报 | 0/3 | 0/3 | 0 |

`python-missed-caller` 和 `ts-exported-caller-and-test` 中，impact map 让模型明确指出未修改的调用方路径；`private-helper-contained` 两版都正确批准。样本只有一个模型、三个题、一次运行，质量分是 harness 的内容评分，不是最终代码正确率；当前证据支持“影响图有 Review 诊断价值，但增加输入”。结合用户确定的取舍规则（质量收益优先于 token 成本），本轮将 C-P11 结论定为默认保留，后续只复核边界和成本，不再把它列为删除候选。

### 当前判断

1. Review impact map **默认保留**：两道跨文件题的影响引用命中从 0/2 提升到 2/2，平均质量分 5.000→6.333；输入成本增加约 26.4%（+836.6 字符），按本审计的质量优先规则接受该成本。
2. Review 本身仍不能从这组 prompt A/B 推出是否值得在完整 Project 每次自动调用；需要比较有/无 Reviewer 的最终编码任务和额外修复成本。
3. 前置 audit 与新项目规划必须和 Review 分开做单变量实验，不能把多个额外模型调用混在一个“完整流程”结果里。

## 阶段 4：编码协议修复 prompt A/B（2026-11-05）

### C-P8 实机结果

原有 harness 排除了 `local` provider，无法直接使用当前 KoboldCpp；这属于审计入口限制，
已在审计期间允许 `local`，没有改生产 provider。使用六个固定协议失败题、同一 JSON
codec、同一模型，比较通用 repair prompt（baseline）与 typed repair prompt（typed）：

| 指标 | baseline | typed | 差异 |
|---|---:|---:|---:|
| 题数 | 6 | 6 | 0 |
| 被解析接受 | 6/6 | 6/6 | 0 |
| 严格单 JSON | 6/6 | 6/6 | 0 |
| 期望动作正确 | 4/6 | 6/6 | +2 |
| clean repair | 4/6 | 6/6 | +2 |
| 平均 prompt 字符 | 7,846.7 | 8,094.0 | +247.3（约 +3.2%） |
| 平均耗时 | 1.523s | 1.346s | −0.177s |

baseline 在 `unknown_write_file` 丢掉了内容末尾换行，在 `invalid_read_offset` 没有修复
offset；typed 两题都按错误字段和固定合同修复。其余四题两版均正确。该实验测的是
协议修复动作，不是完整编码任务；它支持保留 typed repair 的局部提示，但尚不能证明
它减少完整任务的总轮数或总 token。

### 当前判断

1. C-P8 有一组明确 E3 局部收益：正确修复 4/6→6/6，输入增加约 3.2%。
2. 这比“全局缩短 prompt”更值得保留，但需要放入真实 Writer 失败/返修链验证，避免只在孤立协议题中优化。
3. 当前暂不删除通用 repair 兼容逻辑；typed prompt 可作为按错误类型选择的候选，而不是追加到所有请求。

## 阶段 5：项目地图与符号地图导航 probe（2026-11-05）

使用当前仓库、五个只读导航题和本地 KoboldCpp，比较无地图、生产 Project Map、任务
相关 Symbol Map 三种输入。结果：

- baseline 期望路径命中：0；top-1 命中：0。
- project_map 期望路径命中：0；top-1 命中：0。
- symbol_map 期望路径命中：1；top-1 命中：1。
- `symbol_map` 相对 `project_map` 的总命中差：+1。

其中多题模型返回了自然语言说明或 fenced JSON，probe 的严格路径提取将这些结果记为
空路径。该 probe 没有走完整 Writer 的 protocol repair，也没有执行读文件后的真实修改，
所以不能把 0 命中直接当作生产编码失败；它只能说明地图输入没有在这组导航题中形成
稳定、可直接消费的首选路径收益。后续若要研究 C-P6，应改用完整 Writer 任务，以实际
读文件、改文件和最终行为作为判定，并记录协议修复成本。

### 当前判断

1. C-P6 暂缓删除或合并；目前没有证明 Project Map 普遍提高编码质量，Symbol Map 只有局部导航信号。
2. 地图、项目指令、verified facts、checkpoint 和 coding current context 不能只按字符数比较；必须看模型是否少读错文件、少重复读取、少产生无关 diff。
3. 后续完整 Writer 任务矩阵必须包含 bug 修复、功能增加、跨文件重构和从零生成，不能继续只做路径选择题。

## 阶段 6：完整 Project Review gate 与 artifact 边界（2026-11-05）

### JSON/text 协议结果

使用 `tools/local_model_release_gate.py --case project_review --protocol json` 运行真实
Project Writer。Writer 完成了读取、编辑和验证，但第三次模型响应的
`finish_reason=length`，没有形成可接受的终态，也没有进入 Review。gate 正确记录为
`stop_reason=error`、`failure_kind=truncation`；这次结果说明本地模型输出预算/截断是
独立边界，不能把未触发 Review 归因于 Review 逻辑。

### native 协议结果与初始 artifact 误报

第一次 native 运行的生产事件已经满足全部生命周期检查，且独立测试通过：

| 检查 | 结果 |
|---|---:|
| Writer 请求真实存在 | true |
| Review 请求真实存在 | true |
| 验证先于 Review | true |
| Reviewer 只读 | true |
| Review 持久化 | true |
| Review 位于终态之前 | true |
| 最终验证通过 | true |
| 最终文件正确 | true |

但 gate 在复制临时 durable state 作为证据时，遇到恢复清理已经删除的 baseline 文件，
把 `shutil.copytree` 的 `WinError 3` 提升成 `artifact_error`。错误路径位于测试
artifact 复制，不在 Writer、Review、验证或完成持久化路径；因此这次结果应分类为
“生产流程成功、artifact 夹具失败”。

### 修复后的主树复跑

主树提交 `3362fe42 fix(gate): tolerate transient state snapshot cleanup`：

- 项目文件快照失败仍会使 gate 失败；
- durable state 快照中仅由恢复清理造成的缺失文件记录为 `artifact_warnings`，不覆盖任务成功；
- 新增确定性回归覆盖 `shutil.Error` 的 Windows 缺失路径；
- 定向 gate 回归 **21 passed**，Ruff、Mypy（580 个生产源文件）、Pyrefly 和 ty 均通过。

修复后在主树用 KoboldCpp native 复跑 `project_review`：

- `ok=true`，`stop_reason=done`，`exit_code=0`；
- 7 个真实请求（Writer 6 次工具/终态请求 + Reviewer 1 次）；
- 14 项生命周期检查全部为 true；
- 独立 `unittest` 验证通过；
- `failure_stage` 为空，`artifact_errors` 不存在。

### 当前判断

1. Project Review 的 native 生产链路在这次复跑中有完整 E2 证据；自动 Review 的业务收益仍需与“无 Review”单变量对照，不能只凭流程成功保留。
2. JSON/text 失败是本地模型输出截断，属于模型/预算边界；应与生产或 gate 缺陷分开统计。
3. artifact 快照是证据层，必须报告缺失范围，但不能把恢复清理造成的瞬时缺失误报成编码任务失败；这条边界已进入主树。
4. 仍需继续覆盖 bug 修复、功能增加、跨文件重构和从零生成四类完整任务，并分别比较自动规划、前置 audit、Review 和返修的增量收益。

## 阶段 7：四类核心编码任务复核（2026-11-05）

在主树使用同一 KoboldCpp 模型和 native 工具协议运行四类任务：

| 任务 | 首轮结果 | 独立文件/行为验证 | 结论 |
|---|---:|---:|---|
| 已有项目修 bug (`edit`) | PASS | PASS | 模型完成 grep/read/edit/run/done |
| 从零生成功能 (`create`) | 首轮被完成门拒绝 | 文件和 unittest 都正确 | 模型把要求的 `python -m unittest discover` 改成了 `... discover tests`，该命令未匹配选定候选；属于任务命令偏离，完成门的拒绝是安全行为 |
| 跨文件重构 (`references`) | PASS | PASS | 找引用、读取两个文件、编辑和 unittest 均完成 |
| Writer + Review (`project_review`) | PASS | PASS | Writer 验证、只读 Review、持久化和终态前 Review 全部通过 |

首次 create 运行也暴露了一个独立生产边界：空的 `tests/__init__.py` 没有 unified diff hunk，
`edit_integrity` 将它错误标成 `diff_unavailable`。主树提交
`543e5160 fix(completion): observe empty added files` 后，监控器只对 snapshot 元数据明确
标记为 `status=A, additions=0, deletions=0` 的新增零字节文件视为已观测；普通缺失 diff、
二进制和截断 diff 仍保持 unobserved。相关完整性/Project flow 回归为 **135 passed、13
个子断言通过**。

随后把 gate fixture 的命令要求写成精确合同（不得给 `unittest discover` 添加目录或过滤参数），
create 在同一模型上通过：3 个文件正确、`unittest` 通过、`stop_reason=done`，6 个请求，
没有 artifact error。这个调整是测试任务消除歧义，不是放宽生产完成门。

### 当前判断

1. 四类任务的第一轮主链路在修复和消除 fixture 歧义后均有成功样本；还不足以比较自动规划、前置 audit、Review 的净收益。
2. 完成门对验证命令的选择是安全边界：模型自行增加 `tests` 目录参数可能实际通过，但不能自动宣称覆盖了原选定的全量候选。
3. “空文件无 diff”是生产完成证明的真实 bug，已修复并覆盖回归；“命令偏离候选”是模型/任务合同观察，应单独统计，不能与生产 bug 混淆。

## 阶段 8：auto、planning 与测试专用任务（2026-11-05）

继续用主树 KoboldCpp native 跑三个补充轴：

| 轴 | 结果 | 观察 |
|---|---:|---|
| `auto` 从零写 `hello_auto.txt` | PASS | 2 个请求，文件内容精确，终态完成 |
| `planning` 只读规划 | PASS | 1 个请求、无文件变化，模型列出文件和步骤 |
| `tests` 只增加测试 | PASS | 5 个请求，覆盖正数、零、负数和小数，4 个测试通过 |

这些结果只证明入口和完成门能工作，不能证明自动规划本身带来编码收益。`auto` 的两轮
成功也没有与显式 `project` 入口做同题对照；C-P1 仍需要固定任务、关闭规划/开启规划的
单变量 A/B，记录首个正确编辑、总请求和最终行为。

测试专用任务通过说明“只改测试”的场景在当前合同下可完成，但不能因此放宽
`edit_integrity` 的测试修改检查；测试改动是否被用户任务授权、是否有生产文件变化，仍由
完整性和完成证明分开判断。

## 阶段 9：单模型条件下的规划与前置 audit 方法（2026-11-05）

当前只有一个 KoboldCpp 模型时，不能把同一模型的第二次调用称作“独立第二意见”；但仍可
测量规划或前置 audit 这一步本身是否带来净收益。采用同模型、同材料、同任务的因果 A/B：

- baseline：Writer 直接进入项目上下文；
- treatment：先用同一模型生成 bounded plan 或 read-only audit，再把结果按生产
  `ChangeBrief`/audit context 形状交给 Writer；
- 两组都记录最终文件、独立验证、diff 范围、首个正确编辑、请求数、输入/输出 token、
  总耗时和无效调用；
- treatment 的额外调用只回答“这一步是否有用”，不回答“多模型意见是否更可靠”。

因此，单模型可以完成 C-P1/C-P2 的“额外阶段收益”审计；跨模型复核只在以后有第二个
advisor provider 时追加。若 treatment 只增加 token/轮数而没有提高最终代码质量或减少返工，
该阶段就没有保留理由。

## 记录格式

每一步追加日期、范围、事实、证据路径、当前判断、未证明部分、下一步和提交号。
