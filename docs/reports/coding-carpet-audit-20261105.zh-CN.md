# Codey 编码地毯式精简审计（2026-11-05）

> 2026-11-06 证据更正：阶段 19 的生产 headless smoke 归档实际包含 Review unavailable，
> `persisted_review={}`。旧 harness 将一条 Review 事件误当成成功，因此撤回该阶段
> “独立 Reviewer 生产 smoke 已通过”的结论。独立模型的直接 Review/种入缺陷修复证据
> 不受此更正影响。后续严格复核与质量实验见阶段 20。

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
- 当前阶段：六候选的本地单模型审计见阶段 17；后续 Review 实验收口与 KoboldCpp
  兼容见阶段 18。早期阶段保留为历史与诊断记录，不能当最新状态。

### 当前可执行结论

| 候选 | 当前决定 | 本轮依据 |
|---|---|---|
| Project Map | 保留；已有项目有成本收益 | 两个顺序的已有项目题均通过，输入合计约少 25%；从零题并非同样收益 |
| coding current context | 保留与地图的组合，不宣称单独有效 | 单独使用没有净收益；修复真实交付问题后，组合五题全部完成且最终质量通过 |
| 自动规划 | 不强制增加同模型额外规划 | 保留生产上下文时，关闭 2/2，开启 1/2，计划还引入了错误测试期望 |
| 前置 audit | 不强制增加同模型 audit | 保留生产上下文时，关闭 3/3，开启 2/3，请求和输入均增加 |
| 项目记忆 | 保留可信事实/命令；删除只读任务的写入指南 | 冷/暖任务均可完成；笔记正文未被使用，不能声称自动笔记提高了编码质量；删指南双向 16/16 通过 |
| Review / 一次返修 | 保留有界返修与二次 Review | 一类缺陷实际修好；另一类连续漏检，approved 不能替代测试与完成证明 |

生产已接入两项故障修复与一项提示精简，提交为 `7f05b589`、`c4bbb18e`。
这轮支持具体取舍，不支持“每个流程都值得保留”，也不支持删除整个记忆或上下文系统。

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

## 阶段 10：项目地图与 coding current context 的 Writer 级复核（2026-11-05）

### 实验接线

原有 `coding_current_context_ab.py` 的 baseline 两边都传入了 Project Map，因此它不能作为
“地图有/无”的单变量实验。本轮把 Writer 级 harness 改成三个互斥 arm：

- `baseline`：不传 Project Map，关闭 coding current context；
- `project_map`：只传生产 Project Map，关闭 coding current context；
- `context`：不传 Project Map，只打开生产 coding current context。

同时把固定题扩展为 bug、功能增加、跨文件重构、从零生成四类，成功条件仍是终态 `done`、
独立检查通过、changed files 与任务要求完全相等。独立检查命令现在先用
`shutil.which()` 的解析路径启动，Windows `.CMD` shim 与生产 `run` 的语义一致；该接线回归在
`tests/test_coding_current_context_ab.py` 中覆盖。

### 本轮实机结果与失效原因

本轮在 KoboldCpp 当前会话上先跑 `edit-then-verify` 的三个 arm。结果为：

| arm | 结果 | 证据 |
|---|---|---|
| baseline | protocol，0 个工具调用 | 两次空回复，无代码变化 |
| project_map | generation deadline exceeded | 没有可判定的 Writer 结果 |
| context | protocol，0 个工具调用 | 两次空回复，无代码变化 |

前一轮同一模型的独立成功样本中，`context` 曾完成 1/1；更早的三题运行也出现过两边
同时显示空回复。这里的“空回复”来自渲染后的事件，不能证明 provider 原始响应为空。
当时把这些样本统一归为 KoboldCpp 波动并剔除并不充分；阶段 13～14 发现了真实 codec 缺口和
实验包装器改变协议的问题。这批应保留为未完成、归因需核验的历史样本，不能写成地图或
current context 导致失败。完整四类 Writer A/B 仍缺少成对有效样本，
因此 C-P6、C-P7 都没有新的 E3 净收益结论。

当前仍可确认的事实是：Project Map 和 coding current context 都会增加 prompt 输入；已有成功
样本中 context 输入约 94,344→113,872 字符，并没有同时证明最终行为改善。按奥卡姆与质量优先
规则，二者暂不删除，也不能声称已经证明值得保留；下一次实机需在模型输出稳定后按三个 arm
重新跑四类题，并至少取得每类一对有效终态。

## 阶段 11：单模型下自动规划与前置 audit 的可测范围（2026-11-05）

### 生产可用性边界

生产 `advisor_ids()` 会排除当前 Writer provider。当前只有 KoboldCpp `local` 时：

```text
advisor_ids("local", {"local": True}, ("local",)) == ()
```

所以新项目自动规划、已有项目的前置 audit、Writer 后 consensus 在真实生产链路中没有第二个
advisor 可调用；它们会按设计返回无 advisor/继续单模型路径。此条件下不能把同一模型的第二次
请求伪装成独立意见，也不能用“没有发生调用”证明这些机制没有价值。

### 同模型阶段性探针

为测量“额外阶段自身是否有用”，新增 `tests/manual/single_model_stage_ab.py`，让同一个
KoboldCpp 先生成 bounded plan 或只读 audit，再把结果按私有 notes 形状交给同一 Writer；
这只回答额外请求的因果成本，不回答独立 advisor 质量。

`edit-then-verify` 单题结果：

| arm | 阶段输出 | Writer/验证 | 终态 | 总输入字符 |
|---|---:|---|---|---:|
| none | 0 | 无编辑，protocol | 失败 | 35,860 |
| plan | 198 | `app.py` 改对，pytest 通过 | `no_progress` | 97,616 |
| audit | 625 | 无编辑，protocol | 失败 | 39,108 |

`plan` 这次确实帮助模型完成了编辑和检查，但仍没有形成 `done`；其额外阶段和较长上下文
也使请求从 2 次增加到 7 次。`audit` 只增加了一次阶段请求，没有改善 Writer。样本只有一题，
且存在尚未正确归因的协议停机（修正见阶段 14），不能据此合并规划或 audit；它支持继续做成对四类题实验，
同时把“额外阶段导致的终态/请求成本”作为硬指标。

### Writer 后 consensus、Review 返修与项目记忆

- Writer 后 consensus 的生产分支只在 `done`、没有文件修改、且没有前置 audit 时触发；当前
  单模型时 advisor 集合为空，因此没有额外请求。静态分支测试和 consensus/audit 合同回归
  已通过，但没有独立模型的质量 A/B。
- Review 返修的生产链路已有 Project Review native 成功样本，且 `validate_candidate` 会把
  返修限制为一次并要求重新验证与重新 Review。已有 Review/返修定向测试证明生命周期和
  fail-closed 边界；尚未取得“有返修/无返修”同题的真实缺陷留出集，因此不能把 Review 返修
  宣称为净收益，也不能删掉它。
- 项目记忆只在可信 `done`、任务确有变更、检查可信通过且存在文件时写入 implementation
  与 verification notes。记忆、knowledge repository、facts persistence 的定向测试证明了
  写入条件、关联和异常吞吐；尚未完成冷启动/续作同题 A/B，不能证明记忆增加的上下文一定
  值得其 token 成本，也不能据此删除持久化。

## 阶段 12：本轮验证与当前候选结论（2026-11-05）

本轮没有运行全量 `pytest`。针对新增 harness、规划/audit、Review、completion 和 memory
边界运行：

- `171 passed`：consensus、project audit advisor、Review gate、Project completion、
  completion enforcement、knowledge/memory 等定向合同；
- 新增/修改的编码上下文 harness：`5 passed`；
- Ruff：新增与修改的 manual harness、测试全部通过；
- Windows 独立验证接线新增回归：确认 `npm` 会先解析为 `npm.CMD` 再交给
  `subprocess(shell=False)`。

当前候选状态：

| 候选 | 当前结论 | 证据边界 |
|---|---|---|
| C-P1 自动规划 | 暂不合并/删除 | 单模型生产不触发独立 advisor；同模型 plan 仅一题，编辑改善但终态未完成 |
| C-P2 前置 audit | 暂不合并/删除 | 单模型生产不触发；同模型 audit 未改善单题 Writer |
| C-P3 Writer 后 consensus | 暂不合并/删除 | 触发条件已证实，当前单模型无 advisor，缺少质量 A/B |
| C-P4 自动 Review | 暂不合并/删除 | native 生命周期成功；缺少无 Review 的同题最终质量对照 |
| C-P5 Review 返修 | 暂不合并/删除 | 一次返修和再验证边界已证实；缺少真实缺陷留出集 |
| C-P6 Project/Symbol Map | 暂不合并/删除 | 导航 probe 非 Writer；旧 Writer 样本存在协议兼容和实验接线问题 |
| C-P7 coding current context | 暂不合并/删除 | 成功样本输入增加，未取得稳定质量收益 |
| C-P10 项目记忆 | 暂不合并/删除 | 写入条件和关联有 E2，缺少冷启动/续作 E3 |
| C-P11 Review impact map | 默认保留 | 跨文件 Review 诊断命中和质量分提升，成本增加按质量优先接受 |

这批证据不能支持“每个流程都已经证明有用”，也不能支持大规模删除。下一步最有价值的
工作是先排除协议与实验接线问题，再按预注册四类任务继续做 C-P6/C-P7 与 C-P1/C-P2 的成对
Writer 实验，再以真实缺陷留出题验证 C-P4/C-P5，并单独做项目记忆的冷启动/续作对照。

## 阶段 13：重启 KoboldCpp 后发现的真实协议兼容 bug（2026-11-05）

重启 KoboldCpp 后重新运行同一题的三个 Writer arm：

- baseline：真实读取、编辑、pytest、`done` 全部通过；
- project_map / coding current context：首轮都收到空回复并停在 protocol，0 个工具调用。

为了区分 provider 状态和生产逻辑，把两个 arm 分别放入新进程，并记录原始 provider 回复。
模型返回的是完整 KoboldCpp 文本工具帧：

```text
<|tool_call>call:tools.grep{args:{query:"RATE",path:"."}}<tool_call|>
```

`codey/providers/local_response_codec.py` 原先只接受 `grep` 或 `tool:grep`，工具名正则
不允许命名空间中的点号，因此合法的 `tools.grep` 被判成 protocol error。baseline 偶尔
返回 OpenAI JSON/native tool call，所以同一个模型在另一 arm 看起来可以工作；这不是
Project Map 或 current context 本身导致的质量退步，而是提示变化触发了未覆盖的本地文本协议
变体。阶段 14 进一步确认旧包装器没有保留 native 能力，所以上述“返回 native”的描述仅是
当时对回复形状的观察，不能作为已经使用生产 native 协议的证据。

修复放在本地 provider response codec，而不是 kernel 或 Gemma 专属业务逻辑：

- 只接受明确的 `tools.` 命名空间并规范化为 canonical `grep` 等工具名；
- 任意其他命名空间仍拒绝；
- Qwen 等模型使用 OpenAI native `tool_calls` 时继续走 native 解码路径，不受影响；
- 未来若有其他模板变体，再增加独立 codec 分支，不扩大通用工具名正则。

修复后的 project_map 实机已经能完成读取、编辑并通过 pytest；本轮终态停在
`no_progress` 同时伴随模型选择 `python -m unittest`。这个终态本身不足以断定模型能力问题；
阶段 14 继续发现 runner 覆盖判定错误、discovery 等价命令漏判和提示格式歧义，分别修复并复测。

定向回归：`51 passed`，覆盖本地文本 codec、native tool 解码、API context 协议和编码 harness；
Ruff 通过。该生产修复已提交为 `8e5f65c7`。

## 阶段 14：验证命令提示 A/B 的重新归因（2026-11-05）

本阶段直接在 `D:\codey` 工作，目标是减少 pytest/unittest 选择歧义，先做 Writer A/B，
再接入生产提示并跑不替换提示的生产入口。前述阶段 10～13 的“空回复”和 `no_progress`
不能统一归因为 KoboldCpp 波动或模型能力；原始回复、生产覆盖策略和实验包装器均发现了
可具体定位的问题。本节修正此前的原因判断，历史失败样本保留，不与修复后批次混算。

### 已定位的原因和边界

1. **生产验证覆盖误判**：`verification_policy._command_family` 原先把 pytest 与 unittest
   合为 `python:test`，导致 unittest 成功可被算成覆盖 pytest。unittest 不执行普通 pytest
   函数测试，这个替代不成立。现在区分两类 runner；原有 pytest 覆盖 unittest fallback 的
   合同保留，反向替代拒绝。此前探针把 unittest 记成命中 pytest 的统计不能继续引用。
2. **实验接线改变了协议**：旧 `CountingProvider` 只转发 `send`，没有保留 native 的
   `send_turn` / `send_tool_results`，使原本支持 native 的本地 provider 走文本协议。
   改为继承生产 `ProviderAdapter` 并记录 native 回复，增加 native 能力和解码转发回归。
   旧实验不能宣称验证了原样的生产 native 链路。记录的发送文本字符也不等同于实际 HTTP
   输入 token；native 的本地终态收据确认不应被计作模型请求。
3. **编辑合同反馈不清**：恢复 native 后，四次 pilot 都在验证之前失败。模型先没有读取
   文件，随后读取了却把 `base_result_ref` 和 `replacements` 混用，并重复无效调用。
   生产工具描述现在明确二者互斥；错误反馈明确要求删除该 key，而不是改引用值。
   没有自动删除参数、放松 read-before-edit 或变更校验。补清合同后，同样两题 × 两种
   验证提示的四次 native Writer 都以 `done` 完成并通过独立检查。
4. **生产覆盖漏判**：扩展批次旧提示一次把 `(cwd: .)` 抄入命令；改成合法的
   `python -m unittest discover .` 后测试通过，却被完成门拒绝。确定性测试先复现失败，
   再补认同一 cwd 下默认 discovery 的显式 `.`、`-s .`、`--start-directory .` 等形式。
   子目录、`..`、指定测试名和 pattern 筛选仍不算完整默认发现，pytest 也不能由它替代。

### A/B 设计与运行记录

新增 `tests/manual/verification_candidate_prompt_ab.py`，每个 arm 运行生产 Writer adapter、
共享 kernel、本地 native provider 和真实读写/执行工具。两边均关闭 coding current context、
不注入项目地图，仅替换项目上下文末尾的验证候选段。当前模型为 KoboldCpp
`Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，`openai-completions` 传输；
没有第二模型或 seed 对照，不把小样本解释为普遍的能力提升。

- flat：冻结旧的候选平铺文本。
- preferred：沿用现有可信来源和命令优先级，在各 cwd、各运行时中选唯一首选；同分保留
  选项，保留不同目录和语言的验证入口，仍最多展示五条。内部候选集和按实际 diff 选择的
  completion gate 不缩减；配置与成功历史优先级保持不变。
- 新脚本在找不到候选段时直接报错，防止“两个 arm 实际同提示”。CLI 对失败终态或
  首命令未覆盖候选返回非零。扩展题都先运行独立检查确认初始失败，结束后重新检查，
  并要求只修改授权文件和终态为 `done`；原始回复、文件内容和失败尾部上下文均保存。

| 批次 | flat | preferred | 说明 |
|---|---:|---:|---|
| native pilot，未补清编辑合同 | 0/2 | 0/2 | 均在编辑前失败，不能比较命令选择 |
| clear-edit，两类 runner | 2/2 | 2/2 | 两边首命令均匹配且独立检查通过 |
| expanded，反转为 preferred→flat，五题 | 4/5 | 5/5 | flat 一题的 discovery `.` 被完成门漏判；首命令另有 cwd 注释误抄 |

扩展五题为 pytest 配置、unittest 布局、clamp 边界 bug、整数辅助函数功能增加、
根目录 Python/子目录 npm 的 monorepo。补上 discovery 等价形式后的完整复跑、生产提示
接入和不替换上下文的实机结果记录在阶段 15。旧结果没有被成功重跑覆盖。

## 阶段 15：确认批次、生产接入与最终验证（2026-11-05）

### 同题确认与决策

补上 discovery 等价形式后，恢复 flat→preferred 顺序，完整复跑五题。两边使用相同的
修复代码、native 协议、600 秒请求超时和 18 轮 Writer 预算，唯一处理差异仍是验证候选提示。

| 批次 | flat 最终完成/首命令匹配 | preferred 最终完成/首命令匹配 |
|---|---:|---:|
| expanded，补认 discovery `.` 之前 | 4/5、4/5 | 5/5、5/5 |
| confirmation，补认之后 | 5/5、4/5 | 5/5、5/5 |

确认批次 flat 的 unittest 题再次误抄 `(cwd: .)`，随后运行 `discover .` 成功，这次完成门
正确接受并终态 `done`。它的 CLI 仍返回非零，因为脚本同时要求首命令匹配；这不等于最终
编码失败。新提示在两轮五题里最终完成和首命令选择均为 5/5，故接入
`project_adapter._project_context` 的生产 Writer 上下文。

这支持“提示更明确、减少已观察到的命令误抄”，不支持宣称提高了普遍编码成功率、
显著节省 token 或证明所有流程值得保留。修复后的旧提示也能完成全部五题；更早的编辑
合同修正同样对两边生效，其收益不能归给验证候选提示。

新提示增加了来源和使用说明。按当前五题的验证候选段文本计算，pytest 题增加 145 字符，
unittest 题增加 193 字符，monorepo 题增加 223 字符。Writer 探针的 `prompt_chars` 只统计
记录到的逻辑消息文本，未包含完整 native 历史与工具 schema，也可能包括本地终态确认，
不能当作真实 HTTP 请求数或输入 token。这里没有声称节省成本。

### 原样生产复测

接入生产后再运行 `--arm production`，不 monkeypatch `_project_context`：

| 任务 | 首个运行命令 / cwd | 完成 / 独立检查 |
|---|---|---|
| pytest-configured | `python -m pytest` / `.` | done / PASS |
| unittest-configured-by-layout | `python -m unittest discover` / `.` | done / PASS |
| pytest-edge-bug | `python -m pytest` / `.` | done / PASS |
| unittest-feature | `python -m unittest discover` / `.` | done / PASS |
| monorepo-selected-check | `npm test` / `frontend` | done / PASS |

另外运行真实 headless Project 入口，覆盖 Writer 之后的生产生命周期：

```powershell
python -B -m tools.local_model_release_gate --cases edit,project_review --profile smoke --protocol native --timeout 600 --run-dir .e2e-artifacts/coding-verification-production-20261105 --json
```

- `edit`：PASS，6 个 Writer turn，独立 unittest 检查通过，唯一可信完成收据。
- `project_review`：PASS，Writer、一次真实本地 self-review、Review 持久化、只读检查、
  最终验证和终态顺序等 13 项生命周期检查全部通过；不是第二模型独立 Review。
- 两条入口各记录 7 次实际 HTTP attempt，重试为 0；本批没有观察到 KoboldCpp 服务故障。
  这是选定两条 smoke 场景通过，`release_ready=false` 是未跑其他发布场景的范围标记，
  不能改写为全产品发布矩阵通过。

生产 gate 启动时 HEAD 为 `27981df2`，生产提示尚在工作区；归档逐项核对了实际运行时
四个改动生产文件的 SHA-256，并确认其文本与随后提交 `48938eee` 一致，避免仅凭 HEAD
误认运行代码版本。

### 回归、类型检查与归档

没有运行全量 pytest。验证覆盖、completion、工具参数、审计接线第一组为
`188 passed, 115 subtests passed`；扩大到 native 工具、终态收据、项目完成与恢复、
候选刷新失败等另一组为 `125 passed, 77 subtests passed`。两组存在交集，不相加冒充
独立测试数。最终补测生产提示和 native 包装器为 `16 passed`。

新增测试曾错误使用不存在的 `planning` 权限 profile，已按真实合同改为
`planning_readonly`；这是测试编写错误，没有改生产权限去迎合测试。discovery 显式当前目录
回归则先得到六个预期失败，再修复生产覆盖判定，正反例和 completion 有/无 evidence 路径
均通过。

Ruff 检查生产 `codey` 及本轮修改的 harness/测试通过；`mypy codey --python-version 3.11`
检查 580 个源文件通过，Pyrefly 和 ty 同目标版本均无错误。Pyrefly 仍有既存 warning/
suppression，未把“无错误”写成“无警告”。

- 正确性与编辑合同：`27981df2`。
- 生产提示、native 包装器和 A/B harness：`48938eee`。
- [完整实机证据归档](coding-verification-evidence-20261105.json)：保留十个批次的原始
  JSON（包括旧接线 pilot、超时和失败样本）、源文件哈希、精确提示差异、生产 gate 事件、
  原始模型回复及 wire attempt 记录。旧 pilot 不与 native 确认批次混算。

本次验证命令提示优化已完成并接入主树。项目地图、current context、规划/audit、Review
净收益与项目记忆的完整候选 A/B，仍按阶段 12 的未完成范围处理；不能由这次五题推导
“整个编码管线的每个机制都有收益”。

## 阶段 16：六候选真实成本与最终质量 A/B（2026-11-05，诊断记录）

本轮按 [预注册与逐步日志](coding-pipeline-ab-plan-20261105.zh-CN.md) 继续执行六类消融，
不再把导航回复命中、Review verdict 或模型自己写的测试通过单独当成最终编码质量。
独立项目外 oracle 同时覆盖已有 bug、功能、跨文件重构、两个从零任务。

先修复两个实机定位的问题，提交 `7f05b589`：

1. native continuation 跳过了 coding current context 的持续交付。因此修复前 context arm
   不是完整有效处理，原十五行批次仅保留为诊断证据，不能据此裁定 current context 无用。
2. 从零任务运行指定 unittest 文件成功，仍不等于完成门要求的全量 discovery。旧拒绝只给
   泛化“缺少验证”，没有交付所需命令；模型反复 done。现在拒绝时指出实际要求的命令与 cwd，
   不放宽验证覆盖、不自动执行命令。两道题随后都在 6 轮 done、独立检查各 4/4。

确定性回归覆盖 current context 开/关、request-scoped 与其他 native provider，真实创建、
局部验证、被拒 done、补齐验证再 done。扩大恢复、冻结请求、native delivery、失败关闭与
候选选择相关定向测试为 127 passed、3 skipped、20 subtests；三类生产类型检查无错误。

本轮记录真实 HTTP request 和实际输入 tokenizer 计数；当前 SSE 不报告 token usage，
未知为 null，不能写成 0。完整原始回复、wire 请求、usage、工具回执与最终文件逐行保存。
新 harness 首个 pilot 的零用量展示是实验统计错误，不是生产报告零消耗，原始记录保留。
[诊断与首轮原始证据](coding-pipeline-diagnostic-evidence-20261105.json) 包括六个完整批次；
归档只把重复 native messages/catalogs 按哈希合并，逐项重建校验与原 JSON 相等，无删失败。

首轮正式五题中，baseline/map/context 最终成功均 4/5，外部产品行为分别为 20/20；
三者 HTTP 请求总数为 40/43/39，输入 tokenizer 总数为 244053/284935/245105。
地图在已有三题少用请求，但从零 slug 题增加循环，不能只摘有利样本。
slug 的 context arm 自写了错误的 apostrophe 期望，随后试图修改正确实现、反复使用过期
read receipt；其他两边主要重复未授权脚本命令与不可用审批。源码、回执和实际输入均保存，
未发现这些调用被生产协议吞掉；修复反馈能解决部分样本，不保证每次模型都遵从。

固定候选 diff 的 Review 首轮：折扣缺陷在关闭/只审查/审查加返修时，外部质量为
2/4、2/4、4/4，真实返修加二次 Review 共 7 请求。标签大小写去重缺陷三个 arm 均 3/4，
Review 错误 approved，没有触发返修。两份正确控制均无误改。这里分别衡量发现、拦截、
修复与最终代码，不把 Review approved 当正确性证明。尚待反序确认，不改默认 Review 策略。

本阶段之后的固定版本比较、反序确认与生产复测已经完成，正式数值和决策见阶段 17。

## 阶段 17：六候选结论、生产精简与最终复测（2026-11-05，完成）

### 实验与评分边界

当前模型为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，
native 协议、单服务串行运行。候选实验 temperature 为 0.3，没有固定随机 seed；反序确认
用来检查顺序敏感性，不能代替统计显著性或跨模型验证。最终 headless smoke 为生产 gate
配置 temperature 0.0、32K context；精确配置与源文件哈希均随证据保存。

正式证据共 14 个批次，含 107 行任务/控制结果及 8 条实际成功的生产历史。关闭 Review
的固定 diff 控制没有模型请求，不能把 107 行都叫“107 次模型编码”。早期无效处理和失败
pilot 另存诊断归档，没有删除或并入正式收益。

每题分别记录：终态 `done`、授权文件范围、项目外四项产品行为检查、最终可见测试。
最终质量同时要求产品行为与可见测试通过；unittest 发现 0 tests 不算通过。独立检查器
能发现生成测试本身写错，也能发现项目现有测试没有覆盖的产品缺陷。只运行小型隔离项目
的测试，不是仓库全量 pytest。

早期原始行的 `quality_passed` 只含产品行为，没有覆盖生成测试；不回写旧数据，另存
`coding-value-independent-reassessment-20261105.json` 离线重评。下列最终质量均采用
扩大后的口径。请求数为实际 HTTP attempt；候选实验 SSE usage 未报告时保留 null，输入
成本使用实际请求的 KoboldCpp tokenizer 计数，不能把未知当零，也不混用逻辑消息字符数。

### 1～2. Project Map 与 coding current context

两个顺序各五题，覆盖 bug、功能、跨文件重构与两类从零项目。每个 arm 合计十题：

| arm | done + 最终质量 + 范围 | 最终文件质量（不要求 done） | HTTP 请求 | 累计输入 token |
|---|---:|---:|---:|---:|
| baseline | 8/10 | 10/10 | 77 | 464528 |
| map | 8/10 | 10/10 | 79 | 509528 |
| context | 8/10 | 8/10 | 81 | 539291 |

不能只看总表就删除地图：已有项目三题的两个顺序，各 arm 六题均完成且质量通过。
baseline 为 49 请求 / 293910 输入，map 为 37 / 220580，context 为 46 / 273460。
地图在这部分减少约 25% 输入；全样本成本被从零 slug 题的循环拉高。

context 单独两次产生错误的 apostrophe 测试期望，再反复修改正确实现、复用失效 read
receipt。baseline/map 的未完成主要是反复运行未授权脚本，或忽略已交付的 discovery
命令。请求、失败回执均可追溯；没有发现这些分支把工具调用或反馈吞掉。

补跑 map + context 组合五题：**5/5 完成、最终质量与范围均通过，34 请求、221352 输入**。
这说明不能把单独 context 的表现直接外推为默认组合可以删除它；五题组合也不能证明每个
context 字段都有价值。当前保留组合，地图已有项目收益成立，context 单独净收益不成立；
没有据此新增更多上下文，也没有强行删除存在交互作用的投影。

### 3～4. 自动规划与前置 audit

只有一个模型，实验明确测试“额外用同一个模型做建议值不值”，使用生产 prompt、计划
结构和只读 audit kernel；没有注册假的第二 provider。生产无独立 advisor 时的自动跳过
仍保留，以下不能冒充真正多 provider 的独立规划、审计或 Writer 后 consensus 结论。

首轮独立处理：规划两题两边都未 done；audit 关闭 3/3、开启 2/3。进一步在两边都保留
map + context 的条件下重跑，避免其他投影关闭造成混淆：

| 阶段 | 关闭：成功/请求/输入 | 开启：成功/请求/输入 |
|---|---|---|
| 同模型规划，两个从零题 | 2/2；12；74641 | 1/2；17；147222 |
| 同模型 audit，三个已有项目题 | 3/3；18；108757 | 2/3；27；177140 |

规划失败可定位到原始计划：把仅要求替换字符的非 ASCII 示例错误解释成 `hello-world`，
Writer 抄成验收测试。产品实现正确，但生成测试失败，随后反复无效编辑。另一题质量相同，
也多了一次请求。不是测试落后，不修改合同去迎合错误计划。

audit 确认批有一份建议在四轮预算内未报告完成，真实成本照计，不虚构为有效报告。
重构题 Writer 只改定义后反复使用旧 read receipt，未改调用方，独立 pytest 报 ImportError。
核对 wire：只读 audit 历史已清空，Writer 写工具可用，已产出报告也确实交付；没有发现
角色污染或会话接线错误。保持权限、收据和预算，不靠放宽门禁把失败改成成功。

**当前决定：普通任务不强制补做同模型规划/audit，继续保留单 provider 自动跳过。**
独立第二模型、复杂大项目可能有不同收益，仍属未测试范围；本轮已经给出本地现状的取舍。

### 5. 项目记忆：冷启动、真实续作与可落地精简

冷启动两题、真实历史后的续作两题：none 和 memory 各 4/4 完成且最终质量通过。
历史不是手工伪造：标准 headless 入口成功编码后，把事实与 implementation/verification
笔记写入，再重载持久存储。两边在同一路径重置文件，开启组连接真实 knowledge_search/read。

| 场景（每 arm 两题） | 关闭：成功/请求/输入 | 开启：成功/请求/输入 |
|---|---|---|
| 冷启动 | 2/2；12；68000 | 2/2；10；57025 |
| 历史续作 | 2/2；12；68702 | 2/2；10；59373 |

续作可信事实分别注入 312/355 字符，含先前成功验证命令。模型未主动 knowledge_search/read；
核对 kernel 的 knowledge_background 实际请求后，本批也未自动召回笔记正文。因此可以说
这组 memory 路径完成且成本较低，不能说“自动笔记提高了编码质量”。冷启动也不是完全
相同输入：空事实时仍插入 owner/写入 scope 指南，不能把请求减少归因于不存在的历史事实。

由此提炼单变量精简：**没有 knowledge.write 权限的任务，移除知识写入指南**。保持事实、
自动 recall、检索工具与服务端授权；有写权限的任务仍保留原指南。两个顺序的冷/暖共 16 行：

| arm（各八题） | 完成且最终质量通过 | 请求 | 输入 token |
|---|---:|---:|---:|
| 原指南 | 8/8 | 44 | 257814 |
| 只读省略指南 | 8/8 | 43 | 247475 |

固定少 455 字符，每次首请求少约 102～107 输入 token。总输入约少 4.0%，其中也包含一次
请求路径差异，不能全部归功于删字。实际 wire 的指南计数确认处理生效。

生产提交 `c4bbb18e` 已接入 `knowledge_context.py`。真实 scoped recall 回归先红后绿；
有写权限仍收到原指南。接入后无 monkeypatch 的标准 memory arm 冷/暖四题 **4/4**，
23 请求、133155 输入；另外两条真实历史也 done、最终质量通过，全部没有只读写入指南。

**当前决定：保留可信项目事实与成功命令；已删除无关写入说明；本批没有证明自动实现/
验证笔记的质量收益，但也不足以删除其显式检索用途。**完成收据、恢复记录不属于可一起
删除的“模型记忆”，需要独立保持。

### 6. Review / Review 返修的最终质量

固定相同 diff，每题包括人为构造的缺陷候选和正确控制，比较关闭、只 Review、一次返修
加二次 Review；两个顺序均重跑。人为种入的候选不冒充自然 Writer 产出的缺陷分布。

| 缺陷或控制（两轮结果一致） | 不启用的产品检查 | 只 Review | Review + 返修 |
|---|---:|---:|---:|
| 折扣符号正确但漏 0～100 校验 | 2/4 | 2/4，发现问题 | 4/4，实际修好并再审 |
| 标签先去重后转小写 | 3/4 | 3/4，错误 approved | 3/4，漏检所以没有返修 |
| 两份正确控制 | 各 4/4 | 各 4/4 | 各 4/4，无多余修改 |

折扣返修两轮各耗 7 请求、35414/35460 输入，包含重新 Review；正确控制每份多一次审查
请求，约 1100 输入。只 Review 能发现/拦截，但不改变文件，不能直接算质量提升。
本轮是两个缺陷类别发现一个，不能外推成普遍“50% 准确率”。标签缺陷中可见测试全过，
独立 oracle 仍失败，也证明 reviewer approved 和测试绿色都不能单独担保正确性。

**当前决定：保留现有有界返修和新一轮 Review；确有质量收益，同时记录稳定漏检。**
不增加无限返修，不取消独立验证和完成证明；也未声称所有任务都值得同样 Review 成本。

### 已落地主线的根因修复与最终生产验证

本轮确定性问题不是统一归为“模型能力差”：

1. native continuation 没有持续交付 coding current context：生产接线问题，已修复。
2. 完成门要求完整 discovery，但拒绝反馈没有说明实际命令：生产反馈不足，已补命令/cwd，
   不放松覆盖判定。运行指定 unittest 文件本身合法，只是未必覆盖任务要求。
3. 只读编码任务收到无权执行的知识写入指南：提示噪声，A/B 后已按权限移除。

余下失败逐条追到错误计划/测试期望、反复被拒命令、旧 read receipt 或未完成调用方重构，
真实请求与回执存在；本批未发现相应协议丢失，也未观察到 KoboldCpp 断线/重试故障。
这些具体归因不等于证明生产永远无 bug，不为通过实验而删除正确的权限或完成边界。

最终使用提交 `c4bbb18e` 原样生产代码：

```powershell
python -B -m tools.local_model_release_gate --cases edit,project_review --profile smoke --protocol native --timeout 600 --run-dir .e2e-artifacts/coding-pipeline-final-production-20261105 --json
```

- `edit`：PASS、done，5 个 Writer turn，6 次 HTTP attempt；最终独立 unittest 通过。
- `project_review`：PASS、done，7 次 HTTP attempt、一次真实 self-review；13 项生产生命周期
  检查通过，包括 Writer 先验证、只读 Review、持久结果、终态顺序与最终新鲜验证。
- 两条任务均无 HTTP retry；生产文件哈希与运行 metadata 一致。`release_ready=false`
  表示没有运行其余发布场景，不表示这两个选定任务失败，也不宣称全发布矩阵通过。

### 定向回归、检查、提交与证据

未运行全量 pytest。扩大后的组分别为：native/context/gate/恢复 127 passed、3 skipped、
20 subtests；memory/Review/audit/facts 68 passed；headless/完成生命周期 57 passed；
consensus/自动 Review/snapshot/失败不重发 92 passed。组间有重叠，不能相加冒充独立数量。
最终生产提示精简后 knowledge/context/harness 组 69 passed，新增 harness 检查共 6 passed。
收口时再跑 harness、只读知识指南和 native 实际编辑/门禁三组，12 passed；另独立验证
14 批归档可逆、33 份生产文本哈希、639 个生产源文件哈希及报告证据链接，全部通过。

测试错误与产品问题分别处理：新只读指南回归先红后绿，属于真实生产提示问题；一次命令
引用不存在测试文件，确认模块名后修正命令，未改生产；错误生成测试与未完成重构的测试
失败均按任务合同追根因，没有把它们当过时测试删掉。Ruff、Mypy（580 文件）、Pyrefly、
ty 在最后生产改动后通过；Pyrefly 保留既有 35 suppressed / 80 warnings，不宣称零警告。

主要分批提交：`f0aa4503` 预注册；`7f05b589` 两项生产修复；`87ce56cd` A/B harness；
`3dca8d51` 诊断原始证据；`451e2167` 记忆真实检索接线；`0768887d` 组合对照；
`4fb85f44` 最终测试独立重评；`10cc84cf` 阶段证据；`c4bbb18e` 生产精简与回归。

- [正式 A/B、反序、组合、记忆与离线重评证据](coding-pipeline-value-evidence-20261105.json)：
  14 批、107 行、8 条真实历史；675 个不同请求条目，原始 30.25 MB 去重为 7.85 MB。
  按哈希复用请求/消息/catalog，逐项 expand 后与原始解析 JSON 相等；失败、原回复和文件未删。
- [原样生产 smoke 证据](coding-pipeline-production-evidence-20261105.json)：33 份文本制品，
  含原始 provider/events、输入、结果、代码、Review、ledger/trace 与配置/源哈希。
  不含 SQLite 数据库和重复 managed-output；未冒充全 state 镜像。
- [诊断历史](coding-pipeline-diagnostic-evidence-20261105.json) 与
  [预注册/逐步日志](coding-pipeline-ab-plan-20261105.zh-CN.md) 保留修复前证据与演进边界。

六项本地单模型候选审计已完成本轮。仍未验证的是跨模型、真正独立 advisor/consensus、
更大项目和长时间记忆价值，不能将这一局部结论改写成“整条编码管线所有代码都必要”。

## 阶段 18：撤出未用 Review 实验与 KoboldCpp 解析器兼容（2026-11-05）

用户明确允许 Review 漏检：只要修掉可信问题且未引入回归，这轮就有价值；不以找全
种入缺陷验收。共同完成的相同四个候选上，原方案/独立静态/可执行复现分别正常完成且
质量通过 3/4、3/4、2/4，HTTP 10、10、27；正确控制均未被误改。复现组虽发现两类
问题，却发生一次返修语法回归和一次代码正确后未结束。更早开发组的 4/4 未稳定重现。

原生产 Review 保留。提交 `9ab73efc` 已撤出没有正式入口启用的 parent_policy/replay_probes
服务分支、ReviewResult 复现字段及相关持久化；复现 helper 移到 tests/manual，生产
模块不再引用它。只测试被撤分支的测试同步撤出，实验授权/执行/候选冻结测试仍保留。
撤出后 Review/helper/快照/只读/生命周期组 96 passed。

KoboldCpp 1.117.1 在 Gemma edit 文本缺 `]` 时 EOS 后卡住：独立提取安装包原函数，
不导入 Codey、不加载模型，补括号立即返回，原输出 3 秒超时且堆栈锁定 read_array。
根因是遇 `}` 时 read_value 不前进，而 read_array 只等 `]`。这是服务端确定性缺陷。

Codey 的兼容位于 local provider 任务协议选择层：只对已确认版本和 Gemma 4 选择已有
文本工具，避免 API `tools` 触发坏解析器。未开启 thinking 用已有 JSON 约束，开启则保留
设置，并由 local_response_codec 分离完整 Gemma thought/final 段。所有最终调用继续
经过权限、schema、精确编辑、验证新鲜度与完成证明。既有 native 任务恢复不偷换协议。
Qwen、其他版本、未知服务及代理路径不推定受影响；未修改 KoboldCpp 安装包。

首批无约束文本实机失败及 thinking/JSON 对照、第二批跨文件 SEARCH 失败、gate 乱码
误判均保留。gate 修复改用稳定错误码，仍要求源文件确实缺失，权限和目标错误不放过。
最终 `edit/project_review` smoke **2/2 PASS**，后者 13 项生命周期全通过；12 次 HTTP
均没有 API tools，其中 10 次编码请求有 JSON 约束。请求的 native 偏好已降为文本，
不能冒称 native wire 成功。另用默认生产 registry/connector 真改文件、unittest、
done 通过；该轮 Review unavailable 如实降级，不能算 Review 成功。

显式 thinking=true 的默认连接器能解码第一次完整思考/最终段，但下一次又重复 channel
生成，保存现场后人工 abort，任务未完成。这不同于缺 `]` 后的服务端解析死循环；
兼容没有保证该模型所有模式/任务稳定。跨文件题重复字面量 `\\n` 和错误 SEARCH 的失败
也未抹掉，不通过放松编辑守卫或强制关闭用户 thinking 把它伪装成成功。

最终 provider/协议回归 172 passed，JSON/恢复/Review 扩大组 144 passed，gate 修复组
43 passed；组间有重叠。未全量 pytest。Ruff、Mypy（581 文件）、Pyrefly、ty 通过，
Pyrefly 既有 35 suppressed / 80 warnings 保留。

[A/B 与诊断归档](review-reproduction-ab-evidence-20261105.json) 保留 19 个原 JSON，
13.41 MB 去重至 6.79 MB，可逆核对相等；[生产实机归档](kobold-parser-compat-production-evidence-20261105.json)
保留 172 份文本与相关源码，2.93 MB，逐文件哈希核对。不含 SQLite 等完整 state 镜像。
兼容与 gate 错误码修复已提交 `9830a558`；实机期间冻结的 640 份生产和 39 份工具源码
哈希均一致，测试的是这份正式实现。

详见 [独立 Review 与复现逐步记录](review-reproduction-ab-20261105.zh-CN.md)。

## 记录格式

每一步追加日期、范围、事实、证据路径、当前判断、未证明部分、下一步和提交号。

## 阶段 19：跨模型审计——本地 Writer 与 Cline advisor/Reviewer（2026-11-06）

### 实验边界

本阶段按用户指定的连接方式运行：本地 KoboldCpp Gemma4 作为 Writer，Cline 在线模型
只承担只读规划、前置 audit、consensus advisor 或 Review。没有把同一个本地模型的第二次
请求冒充独立意见，也没有把 Cline 的输出直接当作完成证明。使用到的 Cline 模型包括：

- `reflection/beam-free`（Beam）
- `cline-free/nemotron-3.5-super-vl`（Nemotron Super）
- `nvidia/nemotron-3.5-lightning-free`（Nemotron Lightning）
- `cline-free/solar-mini4`（Solar Mini 4）

四个模型均通过真实 Cline 连接返回过响应；这不是四个模型的统计性质量排名。

### 结果

| 项目 | 实机结果 | 成本与边界 | 当前判断 |
|---|---|---|---|
| 独立第二 advisor provider | Nemotron Super 与 Nemotron Lightning 在同一 consensus 题中分别返回独立建议，`advisor_count=2`，未发生降级 | 两个额外在线请求；模型身份和 provider 连接独立记录 | 接线可用；不证明每个任务都值得启动 |
| 第二模型自动规划 | 5 个题中 3 个完成；已有题 `discount-bug`、`tag-feature`、从零 `zero-slug` 成功；`cross-file-rename`、`zero-inventory` 停在 `no_progress` | 每题增加一个 Cline 规划请求；失败题中本地 baseline 分别成功 | 不强制开启，保留为按需候选 |
| 第二模型前置 audit | 3 个已有项目题全部完成，独立测试通过 | 每题增加 Cline 只读工具循环（本批 3～5 个请求）和更长 Writer 输入 | 诊断可用，但没有超过对应 baseline 的最终质量；不强制开启 |
| 跨模型 consensus | 两个从零题均完成；`zero-slug` 的 baseline 在同批停在 `no_progress`，consensus 完成 | `zero-inventory` 产生 2 个 advisor 请求并增加 owner/aggregate 请求；总成本显著高于 baseline | 有潜在救援价值，但样本太小、成本高，保持显式可选 |
| 本地 Writer + Beam Review | 4 个实际 Writer 题中 3 个正常完成；一题 Reviewer 两次返回空/非法 Review JSON，生产解析器正确拒绝 | Review 不会把无效结果写成 approved；该模型不适合直接作为唯一 Reviewer 的证据仍不足 | 记录为模型协议遵循限制，不能归因生产 parser bug |
| 本地 Writer + Nemotron Super Review | 实际题 `tag-feature`、`cross-file-rename` 均完成并返回合法 `approved`；控制题发现确定性大小写去重 bug | Review、Writer 返修、二次 Review 各自独立请求；最终 pytest 和外部 oracle 通过 | 支持保留有界跨模型 Review |

控制题的证据最清楚：人为在 Writer 完成后注入可复现的大小写去重缺陷，未 Review 组为
`quality 3/4`，Nemotron Review 指出具体路径和触发输入，Writer 修复后为 `quality 4/4`，
二次 Review 返回 `approved`。这只证明“发现一个可信问题并修好”有收益，不证明 Reviewer
能找全问题。

证据文件：

- [跨模型规划与 audit A/B](cross-model-planning-expanded-20261106.json)
- [跨模型 Review A/B](cross-model-review-expanded-20261106.json)
- [Nemotron Review 复核](cross-model-review-nemotron-20261106.json)
- [跨模型 consensus 与单模型对照](cross-model-stages-smoke-20261106.json)
- [从零 baseline 对照](cross-model-baseline-zero-20261106.json)、[zero-slug 对照](cross-model-baseline-zero-slug-20261106.json)
- [确定性 Review 返修控制题](cross-model-seeded-review-nemotron-20261106.json)
- [生产 headless smoke](cross-model-production-gate-20261106.json)

### 生产接线发现与修复

审计发现，原生产 Reviewer 选择对本地 Writer 只寻找网页模型；Cline 虽已有连接和模型目录，
却不会被本地 Writer 的自动 Review 选择。已补上一个遵守模型偏好的跨连接选择路径：

- 只有用户在 Settings 中启用并选中的 API 模型才有资格成为 Reviewer；不会自动打开或绕过授权。
- 网页 Reviewer 仍保持优先；没有可用网页 Reviewer 时，才尝试已启用的 API Reviewer。
- 选择的 `connection_id/model_id` 写入 durable state，重启后保持同一 Reviewer，不能静默换模型。
- Reviewer 仍是只读的；Review approved 仍不替代本地验证、diff 和 completion proof。

新增生产入口位于 `codey/providers/api_connections.py` 的
`capture_cross_connection_reviewer()`，选择逻辑位于 `codey/app/review_service.py`。
这次改动只打通了“可以使用独立 Cline Reviewer”的能力，不会让所有单模型任务额外付出
第二模型成本。当前用户状态里的 Cline source 仍是 disabled；启用后才会进入自动选择。

### 当前决定

1. **独立 advisor 接线完成，但不默认强制规划或前置 audit。** 本批规划结果混合且有两道题退化；audit 增加请求却没有提高最终质量。
2. **跨模型 consensus 保留为显式候选。** 它在一个 baseline 停机的从零题上完成了任务，但成本和样本都不足以成为默认流程。
3. **有界跨模型 Review 值得保留。** Nemotron Super 在控制题中找到并修复了可信缺陷；Beam 的空/非法响应说明实际部署必须继续 fail-closed，并按模型观察协议成功率。
4. **Writer + 独立 Reviewer 的“最终质量、成本、回归”已经有第一轮真实证据，但还不是 E4 完成。** 还需要更多留出题、不同任务顺序、不同 Cline 模型和更大项目，才能决定默认 Reviewer 模型与预算。

### 验证

- 跨模型连接、选择、Review 生命周期和偏好边界：`124 passed`。
- 本次新增生产接线的 Mypy、Pyrefly、ty：均通过；Ruff：通过。
- 生产 headless smoke（事后更正）：`done`、exit 0、pytest 通过，但归档为 Review unavailable、
  `persisted_review={}`；没有独立 Reviewer 成功收据，不能证明跨连接 Review 的生产执行成功。
- 未运行全量 `pytest`。

本阶段分批提交：`743bb055`（生产跨连接 Reviewer 接线、合同测试和实机 harness），
`1b5f8588`（本报告与跨模型证据归档）。

## 阶段 20：只看最终质量的扩大跨模型复核（2026-11-06，进行中）

依据用户最新标准，请求成本和耗时不参与删留判断。统一 Writer → 独立 Review → 最多一次
返修 → 二次 Review，再比较增加 plan/audit/一或两个 consensus advisor 的最终文件。
预注册、题目、运行顺序与模型配置见
[本轮方案](cross-model-quality-plan-20261106.zh-CN.md)。

用户进一步明确：主要比较本地 Writer 单独产物与在线合作后的最终产物。修好可信问题且
未破坏原有正确行为即记为本轮收益，不要求找全 bug、全题 oracle 通过或最终 approved。
完整通过率另列，统一 Review 的 stage 对照用于判断额外规划/audit/consensus 的作用。

本轮发现并区分：

- **测试误判**：旧 production smoke 的 `len(review_events)==1` 接受 unavailable；
  新入口要求真实 HTTP、完整 Review、同一独立模型收据、只读快照和最终验证。
  更严格入口的首轮 11 项检查已真实通过，Solar 为 Reviewer；未注入或强制选择 Reviewer。
- **生产边界问题**：私有 audit 暴露用户问题工具，顾问发问后遗留待结算 effect，收尾抛错；
  文本/native 确定性复现均失败，再通过收窄同一工具快照修复，保留统一 kernel 和结算守卫。
- **生产偏好边界问题**：跨连接选择在过滤启用模型前先读取禁用来源目录；
  现先过滤再导入/读取。这个问题被确定性测试证实，不将其冒称旧 unavailable 的唯一原因。
- **夹具接口问题**：新增 TraceProvider 回归最初没有接受 DeadlineProvider 的 timeout 参数，
  造成与目标异常不同的 unknown outcome；修复夹具签名后才得到真实 pending-effect 复现。
- **模型产物问题**：Pilot LRU 产品行为正确，但生成测试的缺省返回值期望错误；
  Writer 重复无变化编辑而停机。保留失败，不手工改成实验成功。
- **上游/模型输出问题**：Super 不在当前目录且返回 404；Solar 曾输出不完整 JSON；
  Lightning 默认思考曾耗尽输出预算。原始证据保留，正式配置明确记录。

生产修复后定向扩大组 119 passed；Ruff、Mypy 597 文件、Pyrefly（0 errors、35 suppressed、
81 warnings）、ty 通过。未运行全量 pytest。正式 A/B 将在修复后的统一源码上重新运行。
