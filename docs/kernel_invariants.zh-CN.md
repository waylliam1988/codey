# 统一任务内核：本轮收敛与可检查的不变量

日期：2026-10-01。基线：`cac74c9`。对应结果见 [TEST_REPORT](../TEST_REPORT.md)。

后续 2026-10-02 的审查、修复与证明限制见 [本轮审查](kernel_review_2026-10-02.zh-CN.md)。以下保留原日期的结果。

后续 2026-10-03 的提交后审查、边界修复与测试迁移见 [本轮审查](kernel_review_2026-10-03.zh-CN.md)。

## 结论的准确范围

本轮已修复审查中复现的确定性问题，最终全量为 **6399 passed、29 skipped、1495 subtests passed，396.40 秒**。

共同模型工具循环、最终完成证明组合入口、原授权恢复和事实投影的职责继续收敛。
生产代码净减少 342 行。不能把这个结果解释为整个项目绝无 bug、死代码或所有真实网页操作都已验证。

“架构收敛”在这里表示重复职责减少、正式所有者明确、已知行为由回归锁定。
它不是一个已经为整个代码库定义了距离函数并证明单调下降的数学命题。

## 入口和函数所有者

```text
用户提交 → 原 TaskPolicy（首次持久化后不可替换）
         → 项目 / 显式 Research / 规划策略，或 auto 首次回答
         → TaskSession + 同一事实视图
         → run_task_kernel
             → TurnSnapshot → JSON / native 归一 → 授权 → execute_turn
             → effect intent / settlement / 受管结果收据
             → completion_gate.evaluate → 一份最终证明

重启：原日志 → 原策略 + 所有已结算观察 → restore_task_session
                                   → 待交付结果（保留原 call id）
                                   → 已交付历史事实（不再次发送这些 id）
```

| 所有者 | 当前职责 |
| --- | --- |
| `policies/task_policy.py` | 只从可信入口建策略；恢复严格校验全部字段，不添加默认授权、不洗类型、不丢拒绝和要求 |
| `runtime/write/mutation_line.py::_replace_task_policy` | 首次设置策略；相同值幂等；已设置后的不同值直接拒绝，原日志不变 |
| `runtime/core/operation_state.py::_parse_task_policy` | 无损保存 JSON 数据；不另写一套领域授权 schema |
| `task_phases/dispatch.py::dispatch_run_mode` | 首个工具/auto 调用前持久化；恢复加载原策略，包括还没有工具结算的运行 |
| `task_entry.py::start_task_session` | 共同入口的任务会话所有者；续跑不得替换策略 |
| `auto_loop.py::run_auto_mode` | 一次首调用；回答过共同门；普通工具 ACTION 和未完成回答均继续同一内核，不通过模式标签重新授权 |
| `recovery.py::recover_effects_for_resume` | 分开处理交付状态和持久事实；读取已结算原收据，不重跑这些工具 |
| `kernel_session_recovery.py::restore_task_session` | 项目及共同入口共用恢复；暂存事实和账本，整批成功才发布；未交付结果只构建一次 |
| `research/ledger_receipts.py` | 编码及恢复精确 Research 观察；完整正文、PDF 页和证据留在现有受管收据中，模型窗口保持有界 |
| `kernel_facts.py::record_facts_for_result` | 实时与恢复共用事实应用；真实 canonical 搜索 URL 经既有来源选择规则生成 ID |
| `task_session.py` / `project_completion_checks.py` / `ExecutionEvidence` | 保存原 command/cwd，按完整身份选择最新观察，不用展示裁剪制造同一身份 |
| `task_loop.py::run_task_kernel` | 共同模型工具循环；两种协议共用执行和完成边界 |

项目 review/repair、ResearchPipeline 及其确定性策略仍有领域职责；不能把它们说成第二个模型工具内核，
也不能宣称它们每个内部操作都已改走 `execute_turn`。显式 Research 保留严格要求；普通联网任务不会自动要求研究笔记。

## 本轮锁定的安全不变量

1. **授权稳定**：持久化后的 grants、denials、required_checks 不被模型计划、模式名称、续跑新请求或重复 setter 改写。
2. **事实与交付分离**：批次 reconstructed 不等于 provider 已收到；批次交付后，工具观察仍属于任务。
3. **结算不重执行**：恢复读取原收据；已结算 edit/run/知识写入不再执行。缺失或非法原收据阻塞。
4. **验证身份完整**：成功、失败和未知使用同一观察路径；command/cwd 不裁剪；旧成功不能覆盖同一身份的新失败或未知。
5. **恢复原子性**：有一条账本/事实收据非法时，不发布已重放的部分结果。
6. **来源精确**：括号和长 URL 保留；摘要中的链接不能成为真实搜索结果；严格 Research 重启后必须拥有原来源与证据。
7. **完成来自事实**：done 只是候选；共同门检查当前要求、工作区身份、验证及严格 Research 条件。直接回答也经过该门。
8. **连续工具任务**：auto 首调用计入总预算；下一阶段保留窗口、任务授权和会话；写锁未取得时不执行。

这些是已编码要求的不变量。开放式自然语言目标的全部含义并没有被形式化，不能把结构化检查通过当成对任意用户意图的数学证明。

## 数学脚本究竟检查了什么

### 1. 有限交付状态图

脚本：`tests/test_kernel_finite_state_invariants.py`。

固定一个 safe 批次和两个 provider effect ID（p1/p2），使用真实 receipt builder 和 projector。
动作集合包含 recovered、abandoned，以及每个 ID 的 send_attempt、send_superseded、delivered。

BFS 从真实初始 intent 开始，以投影状态去重，反复加入所有接受的后继直到队列为空。
本轮达到 **44 个投影状态、134 条接受边**；没有用任意 trace 深度代替不动点。

逐状态检查：delivered 与 abandoned 互斥、已确认 ID 最多一个、确认 ID 必须来自未被 supersede 的尝试、
活跃尝试最多一个、结果引用不变、恢复本身不是终态、终态不会被后继动作翻转。
拒绝动作必须抛出明确的协议异常。

归纳解释：若初始状态满足不变量，且状态投影完整决定后继，且每个接受边保持不变量，
则这个有限模型中的所有可达状态满足不变量。

**边界**：只穷举了上述批次与 ID 集合；没有机器证明投影是所有真实运行历史的充分抽象，
没有证明任意多个批次、任意多次提供者尝试的 refinement，也没有使用证明助手验证 Python、存储或浏览器实现。
因此准确名称是“真实实现上的有限状态穷举检查”，不是全程序形式化证明。

### 2. 能力集合枚举

当前能力词汇为 9 项，遍历全部 2^9 = **512** 个 grants 子集。
每个子集检查两种 denied 情况：空集、全部已授予能力。
检查策略往返精确相等，快照中的 grant 都被允许，JSON/native 的能力相同。
文本批次包装 `parallel/read_files` 降低为正式调用，不在 native 中编造包装 ID。

**边界**：不是全部 partial-denial 组合，也不是对任何未来新增 schema 或外部执行器的自动证明。

### 3. 真存储故障序列

脚本：`tests/test_recovery_trace_invariants.py`。

三种初始验证历史：成功、成功后未知、成功后未知再成功。
与 restart/ack/failure 的全部长度三序列组合：3 × 3^3 = **81** 场景。
使用真实 RuntimeSessionLog、ManagedOutputStore、工作区身份和恢复入口。
独立断言编辑/验证事实相等、执行计数不增加、文件内容不变、未知不完成、新成功可完成。

**边界**：这是有界故障注入。没有覆盖任意进程间并发、磁盘写入的每个硬件时序或外部服务的任意行为。
发送结果含糊时采取保守恢复，不据此承诺外部服务的 exactly-once 交付。

### 重跑

```powershell
python -m pytest tests/test_kernel_finite_state_invariants.py -q -s
python -m pytest tests/test_recovery_trace_invariants.py tests/test_recovery_requires_original_policy.py tests/test_recovery_restores_research_ledger.py -q
python -m pytest tests -q -rs
```

## 删除、保留及性能

删除第二 auto 路由、旧恢复 facade、重复项目恢复包装、`apply_auto_plan`、无消费者的验证候选判定与转发、
旧错误文本恢复判断，以及旧授权查找 helper。删除锁只是防止这些确定冗余回流；正确性仍靠行为测试。

项目 AgentRequest/RunResult 边界、提供者故障切换、网络读取降级、来源 ID 解析、显示名称和非持久化执行器接口仍有真实调用者，
不能仅凭名字含 fallback 就删除。生产网页读取使用 canonical 观察；不带领域账本的显式文本执行器仍有独立结果协议。

全仓顶层私有函数 AST 名称引用扫描已无剩余候选。扫描无法精确判断动态调用、同名成员、插件入口、所有类字段，
所以没有把“扫描无候选”包装成“证明无死代码”。历史 parity fixture 和变更记录保留，不作为生产兼容接口。

生产差异为 **+672 / -1014，净 -342 行**。没有新增持久日志、跨轮扫描缓存或另一套模型循环。
完整来源通过受管收据恢复可能增加存储 I/O；未测真实模型延迟/token，未启用真实 UI 浏览器 E2E。
真实内核和 headless 测试证实同窗继续、原文件读取、取消与预算行为，不能替代真实浏览器体验验收。

摘要/指纹校验还依赖已有摘要算法的实际可靠性、受管存储及可信执行器边界；来源使用截断 text_hash，
相等并不是数学上的内容唯一性证明。全绿也不能排除尚未建模的需求、硬件、网络或 UI 问题。
