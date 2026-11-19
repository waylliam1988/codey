# 主动查询工作图：实机验证与合入准备

本轮验证 agent 能否沿“研究约束 → 实现代码 → 验证反例”检查遗漏。只新增 `tools/`、`tests/` 与报告；没有修改生产代码，没有创建 worktree，没有复制仓库。三个 fixture 各自只有 7–9 个文件；其中一个复制 Codey 的 `codey/types/json_value.py` 单模块，再显式注入“整数解码错误接受 bool”的回归。

**结论：当前候选没有显示足以支持合入的收益。** 完成的显式查询试验中，原流程、文本组、图组都是 0/3 完整成功；图组实际查询 6 次且结果完整进入后续模型请求，仍遗漏关键约束。生产接口贯通和隔离边界已有测试，实际仓库规模检查却失败。保留候选与证据，生产入口继续关闭。

## 实验与证据边界

沿用真实 `run_headless → task_entry/project_adapter → run_task_kernel`，保留模型工具协议、权限、读取前置条件、编辑、执行、完成判断和持久回执。试验层仅在本次 kernel 的资源边界绑定只读 `work_context`，不向 provider 偷塞标准答案，不脚本化实机模型动作。

三组分别为原流程、同语料 BM25 文本工具、图查询工具。文本/图具有相同工具名、参数、描述、节点语料和 7,000 字符上限；区别是关系扩展及缺口输出。baseline 没有该新增工具。三个任务都保留旧测试，要求新增 `tests/test_regression.py` 并执行完整 `python -m pytest -q`。

| 任务 | 独立产品检查的重点 | 研究来源 |
|---|---|---|
| checkout | 渠道、折扣、零截断、Decimal 舍入 | fixture 中的明确合同 |
| relay | 版本、方法/状态、ASCII 字符串、默认值与范围 | 真实生产 research ledger 记录的受控网页正文 |
| codec | 整数身份拒绝 bool，其他解码与 JSON 安全保持 | fixture 合同和复制的 Codey 模块 |

受控网页用于验证“搜索/打开正文 → 已提交账本 → 动态查询 → 编码”，不是实网浏览器自动化。离线 scripted transport 测试也不计入模型能力。

评分分层：产品行为及变更范围正确；新增回归在最终实现通过、恢复原 bug 后真实失败；最后还须生产 kernel 正常 done，并持有当前工作区的完整 pytest 成功回执。外部 grader 和突变检查结果不反馈模型或图。通过旧测试、说“已验证”、静态导入关系都不能独立通过。

模型固定为本机 `Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，temperature 0，native tools，最多 16 轮，每次最多 4,096 输出 token，单次物理尝试。冻结任务、评分器、脚本及生产 Python 哈希，串行请求。

## 实机进展

可选工具试验原计划 18 单元，实际保存 5 个完整单元后提前停止：3 个 checkout、flat/graph 两个 relay；所有单元均为 **零次图/文本工具调用**。checkout 都遗漏半入舍入要求；relay 都遗漏部分无效输入约束。不能把这个未完成矩阵当作图效果比较，更不能把“不采用工具”解释为“图算法无用”。

原客户端在首次 relay-flat 第 9 次请求后消失，后端已完成生成但客户端未保存响应，原因未知。保留原目录到 `interrupted/r0-relay-flat-attempt1`，从冻结初态重跑该单元；不是挑掉行为失败。另一项浏览器试验随后并发使用模型，relay-graph 耗时受排队影响；runner 在该组保存后发现后端忙，正常停止，没有中止其他生成。耗时不作收益依据。

另冻结显式查询试验：三个组使用相同的条件式指导——在有 `work_context` 时，在读取资料后/编辑前和测试后查询 `view=gaps`，逐条比较约束、实现与反例；没有该工具时用原工具完成同样核对。不给具体答案。修正 relay 任务中“仅修改源码”与“新增测试”的矛盾措辞。该协议独立计分，不与上一轮混合。

显式查询试验期间浏览器任务也续跑了，出现共享服务排队；本实验自身仍逐单元、逐请求串行。外部续跑脚本只在空闲时启动或继续冻结 runner，遇忙退出后等空闲再续，不重跑已保存的行为失败。所有组耗时均不用于性能结论。

显式查询试验的 9 个单元全部完成；每任务每组只跑一次，属于小样本探索，不外推所有模型或图方法。

| 任务 | 原流程：产品检查 / 请求 / 停止 | 文本组：产品检查 / 请求 / 查询 / 停止 | 图组：产品检查 / 请求 / 查询 / 停止 |
|---|---|---|---|
| checkout | 4/5 · 10 · done | 4/5 · 12 · 2 · done | 4/5 · 13 · 3 · done |
| relay | 4/5 · 16 · max_turns | 4/5 · 12 · 2 · done | 4/5 · 11 · 2 · done |
| codec | 4/5 · 14 · no_progress | 4/5 · 8 · 0 · no_progress | 4/5 · 8 · 1 · no_progress |

checkout 全部遗漏半入舍入。图组在测试后真实查询，结果同时呈现 ROUND_HALF_UP 原文、候选测试计数和当前工作区 pytest 成功；模型仍未补能揭露此错误的反例。这是“已交付并主动查询，仍未正确采用”的直接负结果。不能把多查三次本身当作收益。

relay 全部遗漏部分无效输入：都使用 `.isdigit()`，未正确限制 ASCII。文本/图组正常完成、当前完整 pytest 通过，baseline 花满轮数且缺少当前完整成功回执；这是本轮完成流程的差异，不能替代产品正确性。两工具组都在编辑前和最后 run 后查询。

codec 全部未修复 bool 身份错误，生产内核均以 no_progress 停止。图组查询一次，文本组没有按条件式指导查询；这也作为未采用工具的结果保留，不能只挑采用的单元计算成功率。baseline 写了会失败的回归但未完成修复，另外两组没有有效改动和新增反例。

| 汇总 | 原流程 | 文本组 | 图组 |
|---|---:|---:|---:|
| 产品 / 目标 / 端到端完整成功 | 0/3 · 0/3 · 0/3 | 0/3 · 0/3 · 0/3 | 0/3 · 0/3 · 0/3 |
| 主动查询 | 0 | 4 | 6 |
| 模型请求 | 40 | 32 | 32 |

请求较少的一部分来自 codec 提前停止，不能解释成效率提升。9 单元没有 wire 错误，冻结 runner 和生产哈希均匹配；10 次成功查询均有持久回执，且按 native tool_call_id 核对原始出站请求，完整查询 payload 确实进入后续模型上下文。没有将“构了图但模型未收到”混淆为采用失败。

评分记录曾只读取 execution receipt 的 `output`，但这条生产路径将正文保存在 `ToolResult.model_text`，导致真实 pytest 成功被漏计。修正为 `output or model_text`，补回归测试；归档原 runner、原协议并记录 instrumentation amendment。旧 summary 不覆盖，由 `audit_work_graph_query_probe` 从同一已保存正式观察恢复显示正文，写独立 `audited-results.json`；工作区身份、exit code、ok 等权威字段仍取原生产记录，不能从显示文字重新推导。

## 与生产的贴合程度

| 已实测的接口或边界 | 证据 |
|---|---|
| 原生工具及 JSON 两种生产路径 | 同一个真实 headless/kernel 执行图查询，只有 transport 离线回放 |
| 本轮正式来源动态加入图 | open 前只有 URL，open 后从真实 ledger 克隆正文；精确摘录 span/hash |
| 正式执行与版本失效 | 真实 pytest 失败后入图；编辑后旧结果非当前；重跑通过后新结果为当前 |
| 读取/编辑权限与来源隔离 | project.read、web.read，忽略路径、越界参数、扫描预算；图片段不授予编辑读取凭证 |
| 持久查询回执 | 用生产 RuntimeEffectStore 与 restore_result_receipt 还原正文、canonical、call_id |
| 按 run 绑定服务 | `work_graph_run_binding.bind_work_context` 使用现有 KernelExecutionDeps.executors，两个项目共用定义却各自使用自己的服务；故意设置的全局错误服务从未被调用 |
| 产品评分不依赖模型自述 | 外部行为 grader、变更范围检查、恢复原 bug 的反例敏感性检查 |

候选图自动抽取 Python AST 导入/符号、文档路径/URL、英文约束原文和真实执行观察。关系分别称为 `applies_to_candidate`、`related_test_by_import`、`test_anchor_overlap_candidate`、`observed_failure`。**静态相关关系不等于行为覆盖**。目前 `gaps` 坦率返回 `behavioral_proof_not_established` 和候选测试；它让模型有机会核对遗漏，但不自动判定哪条约束已证明。

checkout 轨迹也显示了这个区别的实际意义：新增测试后，舍入约束的词汇锚点候选数从 0 升到 4，然而这些测试仍没有击中半入舍入反例。不能把计数增加或整套测试通过当作约束覆盖。若后续继续，应记录“精确约束版本 → agent 声明的具体反例输入/预期 → 测试版本 → 真实执行”的逐条联系，区分模型声明和观察；不要把当前泛化提示包装成自动查漏证明器。

codec 图组查询后未修改成功：两次 edit 的 old/new 完全相同，指向允许 bool 的另一段 JSON 规范化逻辑，最后被生产 no_progress 停止。该规范化逻辑本来应该接受 bool，真正目标是 `require_json_integer`。这也暴露查询粒度不足：当前“条款 → 模块”的粗连接与前缀片段可能加强错误定位。后续应优先返回目标限定符号、具体调用关系与相关例外，再建立反例联系；不是给所有相邻函数一视同仁地附上同一约束。

离线 41 项测试通过。10 个相关 Python 文件的 Ruff、mypy、Pyrefly、ty 均通过，类型目标 Python 3.11；[最终完整日志](D:/codey-local-evidence/agent-work-graph-query-offline-20261119/validation-final.json)已保存。pytest 清理 RAM 盘曾出现路径已不存在的 warning，不影响断言与退出码；未修改通用测试设施。

补做了一项事后生产贴合检查：复用现有 `tests/test_json_value_contract.py`，只将被测 codec 的 import 改到各组最终副本，在模型结束后从项目外执行。三组都通过这 12 项既有契约检查，但全部仍保留整数身份错误。这说明保留既有行为与修复新约束是不同门槛；这项补查不替代冻结产品 grader，也不事后改变主评分，更不是模型产生的测试。衍生脚本、源/改写后 hash 与结果保存在证据目录。

直接读取 `D:\codey` 的规模检查暴露了实际阻碍：生产 workspace fingerprint 扫描报 `directory_entry_limit`，因此完整查询不能建立当前身份。进一步只构建无权威身份的诊断索引，根目录临时文本又消耗了 2 MiB 扫描预算，最终只有 1 个节点，目标 `codey/types/json_value.py` 不在索引中。这份诊断不能作为正式验证。**候选目前不具备直接合入条件**；小 fixture 证明了接口能贯通，没有证明真实仓库可用。不得通过去掉预算、伪造指纹或复制干净仓库掩盖问题。

## 合入准备与尚未完成的工作

目前可审查的候选分为：图构建/查询纯逻辑、真实生产观察适配器、按 run 的 executor 绑定，以及实机/离线验证。没有新增数据库、后台模型或新权限；可通过是否注册工具关闭整个入口。

正式接入时，应将稳定 schema 在进程启动时注册，**服务只绑定到各自 request.execution.executors**。本轮 live runner 为串行隔离实验暂用全局 executor 的 context manager；不能照搬到多会话生产。已用单独候选接口和真实 kernel 测试验证 per-run seam，但 schema 注册生命周期仍需在正式接入时拆分。不要以每个会话注册/注销全局定义来处理并发。

合入前首先解决并回归验证上述规模失败：超大文件应在消耗正文预算前过滤；优先从当前任务、已读资料和显式目标路径建立有界邻域；扫描覆盖不足时清楚返回不可判定，并沿用生产身份失败规则。不得为了让图工作而悄悄放松整个生产执行凭证的含义。

另外需要在合入时完成：为内部 session 观察建立明确只读接口；检查持久化与幂等投影；完善来源失效、同名/限定符号身份、相对导入；在实际仓库规模验证增量更新、取消与查询延迟。本轮重建索引上限是 400 文件/2 MiB，单文件 64 KiB，符号定位仍用行号。覆盖只有 Python 和英文规范句；不是完整代码索引，也尚未接跨会话知识沉淀、TypeScript、实网浏览器恢复。

不能把节点遍历换个名字就视为生产收益。进入生产试验的依据应该是：模型确实调用了图，轨迹显示它因此发现原本遗漏的约束/反例，独立行为和敏感回归均通过，并在同预算的文本组之外有可重复改善。小样本若没有显示收益，就保留测试与候选，继续保持生产入口关闭。

UI 可直接消费同一查询回执：按“原文约束 → 候选实现 → 反例测试 → 对应工作区执行”展示，明确标识静态候选、真实失败、旧结果及截断。用户点击缺口时，应交付精确节点/版本给 agent 复核。此次没有把 UI 原型混入模型收益实验。

## 重跑

先确认模型空闲；使用新的证据目录，不复制仓库，不创建 worktree：

```powershell
python -m tools.agent_work_graph_query_probe --root D:\codey-local-evidence\graph-optional-new --prepare --repeats 1
python -m tools.agent_work_graph_query_probe --root D:\codey-local-evidence\graph-optional-new --run
python -m tools.agent_work_graph_guided_probe --root D:\codey-local-evidence\graph-guided-new --prepare --repeats 1
python -m tools.agent_work_graph_guided_probe --root D:\codey-local-evidence\graph-guided-new --run
python -m tools.audit_work_graph_query_probe --root D:\codey-local-evidence\graph-guided-new
```

原始证据：

- [可选工具协议及中断说明](D:/codey-local-evidence/agent-work-graph-query-20261119-v1/protocol.json)、[提前停止记录](D:/codey-local-evidence/agent-work-graph-query-20261119-v1/study-status.json)、[审计评分](D:/codey-local-evidence/agent-work-graph-query-20261119-v1/audited-results.json)。
- [显式查询协议](D:/codey-local-evidence/agent-work-graph-guided-20261119-v1/protocol.json)、[主汇总](D:/codey-local-evidence/agent-work-graph-guided-20261119-v1/aggregate.json)、[审计评分](D:/codey-local-evidence/agent-work-graph-guided-20261119-v1/audited-results.json)、[真实交付及哈希审计](D:/codey-local-evidence/agent-work-graph-guided-20261119-v1/trace-audit.json)。
- [事后复用生产 JSON 契约测试结果](D:/codey-local-evidence/agent-work-graph-guided-20261119-v1/production-contract-crosscheck/results.json)。
- [评分器离线校准](D:/codey-local-evidence/agent-work-graph-query-offline-20261119/grader-calibration.json)。
- [真实 Codey 仓库规模失败记录](D:/codey-local-evidence/agent-work-graph-query-offline-20261119/production-scale-smoke.json)。
- 每单元保留 wire 请求/响应、工具事件、来源获取、图查询、持久回执、最终子图、生产执行检查、外部反例检查与小型项目。
