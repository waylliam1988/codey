# Linger 对 Codey 的借鉴分析

分析日期：2026-11-20。Linger 已完整克隆到 `D:\codey\reference-projects\linger`，保留独立 Git 历史。
来源为 <https://github.com/simplexai-labs/linger>，本次快照为
`7494f818424b5b4cb8b79a9d412212583bdb72aa`，最近提交为
`feat(teamhub): add Outlook Calendar polling listener`。
Codey 阅读基线为 `621e3ce98ecacf9e6ecd6eb8715fcdf712e53dde`。
参考目录已被现有 `.gitignore` 忽略，无需把上游源码纳入 Codey 仓库。

**最值得学的是：让用户过去的经历真正帮助下一次工作，并让这种能力不拖慢、不打扰当前任务。**
具体优先做旧经历检索、可修正的长期记忆和前台优先的资源调度；安装诊断和跨语言协议也值得借鉴。
Linger 的多服务、全桌面控制、自动夜间生成和多 Agent 产品形态，不适合整体搬进 Codey。

本文基于两边源码和文档，另对 Codey 检索窗口做了临时数据库探针。没有安装或启动 Linger 全栈，
没有复测其模型质量、推理速度或真实 UI；下面区分已看到的实现、Codey 的现状和实施建议。

**两个项目的目标和基础不同。**

| 维度 | Linger | Codey 当前实现 | 对借鉴的影响 |
| --- | --- | --- | --- |
| 产品中心 | 个人助手、Rooms、连接应用、文档、主动提醒 | 低成本、本地优先的编程与研究工作台 | 学连续性和交互质量，控制功能扩张 |
| 模型接入 | 本机 Codex / Claude / Pi 等 Runtime，支持本地模型与 BYOK | 网页模型适配器与共享 API runtime | 网页发送不确定、登录和限流边界仍需 Codey 自己处理 |
| 运行结构 | Go TeamHub、Node services、PostgreSQL、本机 Go Daemon、React UI | Python 共享服务、durable SQLite、统一任务内核、TypeScript UI | 可以移植机制，无需引入同样的服务部署 |
| 记忆 | 个人 Wiki 与 Agent memory；自动抽取、整理会使用额外模型调用 | Ghost 是独立合同后的 SQLite 投影、词法检索与时间衰减 | 检索可以直接研究；自动生成与零额外生成目标冲突 |
| 执行边界 | 按 Runtime 和操作提供不同 guardrail | 工具权限、审批、来源、完成证明和恢复由 Core 拥有 | 保留 Codey 的统一所有权，不让记忆获得执行权 |

定位依据：[Linger 架构](../reference-projects/linger/docs/architecture.md)、
[本地模型说明](../reference-projects/linger/docs/local-inference.md)、
[Codey 产品原则](../PRODUCT_PRINCIPLES.zh-CN.md)、[当前 Memory 边界](memory-boundary.zh-CN.md)。
旧 Ghost 路线图中的 Hebbian、Hopfield、自动提取和学习循环不是本次 Codey 基线的当前实现。

**建议优先级如下。P0 是最先验证和解决的具体问题，P1 随后推进，P2 保留为可选研究。**

| 优先级 | 应借鉴什么 | Codey 的最小落点 | 用户收益 |
| --- | --- | --- | --- |
| P0 | 搜得到旧经历，并能回到原始来源 | Ghost 的索引与候选检索，保持现有来源准入 | 早先讨论过的重要决定不会仅因聊得多就消失 |
| P1 | 长期理解可替换、可撤回、解释得清楚 | 现有 proposal / decision 路径上的版本与替代关系 | 改过的偏好不再与旧偏好同时影响回答 |
| P1 | 小摘要、按需读取、稳定提示前缀 | Core 拥有的上下文投递与有界读取 | 长期积累不等于每轮塞入更多内容 |
| P1 | 当前聊天优先，后台维护让路 | 共用资源准入与现有 provider 生命周期 | 后台工作不抢占用户正在等待的模型 |
| P1 | 连接验证、故障恢复与诊断闭环 | 现有 Settings、健康查询与本地诊断导出 | 更容易第一次用成功，也更容易定位失败 |
| P1 | 跨语言协议单源与用户行为验收 | Python / TypeScript 公共事件及 DTO 边界 | 少一些后端成功、前端误解的状态问题 |
| P2 | 从纠正中学习、后台委派与主动提醒 | 先做有来源的候选和有限只读任务 | 有明确需求与成本证据后再扩大能力 |

**1. 最先学检索方法：保存、搜到和回答正确，是三件不同的事。**

Linger 的 `knowledge-query.ts` 把原始资料与派生 Wiki / 报告分别排序，避免长摘要把原始证据挤掉；
检索结合词法查询、替代表述和时间分支，用 RRF 合并排名。时间窗口贡献排名，不直接过滤其他结果。
这套检索本身不依赖 embedding 或额外 reranker 模型。模型自动生成 Wiki 和事实则另有成本。

源码：[双路检索](../reference-projects/linger/apps/node-services/src/memory/knowledge-query.ts)、
[RRF](../reference-projects/linger/apps/node-services/src/memory/search/rrf.ts)、
[时间覆盖](../reference-projects/linger/apps/node-services/src/memory/search/temporal-window.ts)。

Codey 当前 `GhostMemory.retrieve()` 先取最新 200 条 experience，再计算词法相关性。
已确认的 preference / goal / hypothesis 单独进入候选，不受这 200 条限制。
`retrieval.py` 的相关性是 query token 覆盖率乘时间衰减，尚无全文历史索引或替代表述融合。

本次直接调用真实 Ghost 后端，以临时 SQLite 写入一条数据库迁移备份经历，确认能搜到；
再写入 200 条按钮颜色和页面间距经历，重复原查询，结果变为空。
导出确认共 201 条，原经历仍然在库里。这是有界候选窗口的可复现取舍，不能等同于数据丢失。
探针只验证后端召回，不覆盖 Core 来源准入，也不证明真实模型回答质量。

建议先让全部仍有权使用的经历进入可检索索引，再从索引取有界候选，而不是仅扩大 200 这个数字。
可以评估 SQLite FTS5 与中文二元词索引，先确认运行环境能力；无需为了检索引入 PostgreSQL。
在此基础上比较“相关性单路”与“相关性加近期分支”，确有收益再加 RRF。
同义改写或中英混合仍是词法检索的限制；不要把 Linger 允许模型传入替代表述误写成零成本自动语义搜索。

验收至少包括：超过 200 条后的旧经历、同义问法、中英混合、明确日期、无答案、来源删除、跨项目隔离。
同时检查 scope、当前分支、expiry、发送前复核和总预算；搜索不到时不能直接断言“你从没说过”。
落点：[后端](../codey/ghost/backend.py)、[排序](../codey/ghost/retrieval.py)、
[Core 过滤](../codey/memory/service.py)。

**2. 学可修正的长期状态：新理解要有替换关系，旧经历仍保留原貌。**

Linger 更新实体 Wiki 时，摘要可以 keep 或带 reason 重写；列表通过稳定 item ID 添加、修改、撤回或合并，
未提及项保持原样。应用生成 Change Log，并检查重写后保留的引用，以及删除引用的说明。
这是结构和来源覆盖校验，不能保证每段模型改写都忠实，仍需要行为与内容验收。
源码：[页面更新与校验](../reference-projects/linger/apps/node-services/src/llm-wiki/wiki-page-template.ts)。

Codey 已有 `MemoryProposal`、版本匹配、用户决定来源和幂等决定路径，这是可靠基础。
但当前接受新项主要是插入，合同没有表达“新偏好替代哪一条旧偏好”的显式关系。
如果先接受“回答详细些”，后来接受“默认简洁”，不能只让时间衰减和排序替业务冲突作决定。

建议先在现有人工提案流程明确替代对象、作用范围和撤回行为：确认新版本后，旧版本退出正常候选，
同时仍能从 Core 决定记录追溯原因。只增加真实用例需要的字段，不先造通用知识图谱或自动冲突裁判。
原始经历、用户确认的偏好、助手提出的假设保持身份区别；研究结论仍由知识与证据所有者管理。

验收用同一用户先后纠正的真实场景：新偏好有效、当前请求可覆盖它、其他项目不受影响、重启后保持、
撤回后不复活；不能只检查“写入成功”。落点：[记忆合同](../codey/memory/contracts.py)、
[Ghost 后端](../codey/ghost/backend.py)、[来源装配](../codey/app/memory_setup.py)。

**3. 学按需上下文：把历史做成可找到的资料，而不是越积越长的前言。**

Linger 的 Agent memory 把 `summary.md`、`MEMORY.md` 和 session summaries 投影为只读文件。
短索引在新会话及 compaction 后的首条输入中附带，不随每次发布改写 developer instructions。
这为稳定会话前缀和按需读取创造条件；实际缓存收益仍由具体 provider 决定。
源码：[只读投影](../reference-projects/linger/apps/node-services/src/agent/agent-features/agent-memory/projection.ts)、
[投递时机](../reference-projects/linger/apps/node-services/src/agent/agent-features/agent-memory/feature.ts)。

Codey 已有完整候选选择、Context Epoch 和普通请求 6000 字符记忆预算，不缺“有界”这层机制。
借鉴方向应是改善预算内的信息价值：少量相关条目附原始来源定位；确需展开时，由 Core 提供有界、
可验证来源的读取。Ghost 仍返回结构化 item，不返回 prompt，也不直接决定读文件或调用模型。

先测长经历在现有预算下的命中与完整投递，再决定是否需要短索引。不要为了生成摘要恢复额外抽取调用，
也不要新增绕过工具范围的任意记忆文件读取。只读文件投影本身不等于来源撤回后的安全保证。

验收比较输入 tokens、首字延迟、实际回答、工具读取次数，以及撤回后旧摘要是否仍会被使用。
落点：[原子候选选择](../codey/workspace/context_selection.py)、
[准入](../codey/workspace/context_admission.py)、[Memory 所有权说明](memory-boundary.zh-CN.md)。

**4. 学交互优先的调度：先限制竞争，再考虑复杂的自适应与抢占。**

Linger 用统一策略表给 chat、task、compaction 和 memory 排序；每个 Computer / Runtime endpoint
有一条资源队列，同优先级按到达顺序。容量大于一时为交互保留最后位置，运行计时与排队计时分开。
还实现了根据延迟和拒绝反馈调整并发，以及在可恢复条件满足时让低优先级工作让出资源。
源码：[策略表](../reference-projects/linger/apps/node-services/src/model-tasks.ts)、
[队列](../reference-projects/linger/apps/node-services/src/agent/runner-gateway/model-queue.ts)、
[自适应限制](../reference-projects/linger/apps/node-services/src/agent/runner-gateway/adaptive-limit.ts)。

Codey 已有 durable scheduler、API transport 的有界并发和后台 compaction；这些分别解决任务持久性、
传输资源和上下文维护，并不等于已经有上述统一的模型工作优先级。当前 Ghost 没有模型调用、worker
或 scheduler，不应为借鉴调度而把这些职责重新塞给 Ghost。

建议先测后台 compaction 与前台请求争用同一模型的延迟，再按实际连接容量提供简单的资源准入。
共享资源的身份应来自真实连接，而不是照抄 Linger 的 endpoint 定义。
先做“新后台步骤等前台”，有可恢复检查点后再研究让出资源；网页生成状态不确定时不能中断重发。
排队截止时间与实际执行超时分别记录，审批等待也不能被误算成模型执行。

验收包括单容量模型下的聊天排队、后台连续步骤、取消、审批、饥饿和断线恢复；比较 p50 / p95 等待和
首字延迟，并确认没有重复请求或重复工具执行。落点：[scheduler](../codey/durable/scheduler.py)、
[compaction](../codey/providers/compaction.py)、[transport](../codey/providers/api_transport.py)。

**5. 学首次使用与诊断闭环：把已有的正确边界变成用户能完成的路径。**

Linger 不只提供模型选择，还提供连接 Test、运行环境 doctor、安装恢复，以及可预览文件清单的诊断包。
诊断代码排除凭证、Cookie、数据库等文件，处理 secret 字段与用户目录路径；发送是用户主动动作。
源码：[诊断包](../reference-projects/linger/apps/desktop/src/main/diagnostics.ts)、
[诊断协议](../reference-projects/linger/docs/diagnostics-upload.md)、
[Computer doctor](../reference-projects/linger/apps/computer/README.md)。

Codey 已有 `ProviderFailure` 类型、网页 Profile Doctor、运行 trace 和 control health。
更值得补的是把这些能力汇成普通用户可操作的失败路径，而不是再做一个技术信息常驻面板。
例如明确区分：本地服务未启动、网页未登录、需要人工处理验证、输入尚未提交、提交结果未知。

建议以“安装 → 选模型 → 第一次回答 → 第一次审批 → 看 diff → 恢复”作为完整验收；
连接测试由用户显式触发，不能在选择模型时偷偷发请求。
诊断先做本地导出和内容预览，复用现有结构化事实，默认不打包聊天正文、项目源码、凭证或数据库。
暂不需要托管上传服务。落点：[故障类型](../codey/providers/diagnostics.py)、
[控制协议健康查询](../codey/control/protocol.py)、现有 Settings 与 Run details。

**6. 学协议单源与行为验收：类型一致和用户成功都要验证。**

Linger 的跨进程 OpenAPI 生成 Go / TypeScript 消费者，verify 会重新生成并比较，防止 schema 漂移。
本地与 CI 共用 `check-plan.mjs` 选择受影响检查；无法判断时选择全部检查。
Web 还有生产初始 JavaScript 依赖图的体积预算测试。
源码：[contracts](../reference-projects/linger/contracts/README.md)、
[检查选择](../reference-projects/linger/scripts/check-plan.mjs)、
[构建预算测试](../reference-projects/linger/apps/web/src/test/build-assets.test.ts)。

Codey 已有机器契约门、严格类型检查、CLI / 桌面共用内核和前端解码，不能说它缺少契约或测试。
具体增量在 Python 公共事件与手写 TypeScript `ServerEvent` 的双边维护；目前该类型以大量可选字段
表达不同事件。可先为一组状态事件建立单一 schema，生成可辨别事件类型并保留运行时验证，逐步推广。
同进程的 Memory Protocol 没必要因此升级成跨进程服务。

质量验证值得学 Linger 的 memory lifecycle：它用多天历史检查文件版本更新、偏好纠正、待办关闭和无答案。
其 harness 复用当前产品函数，但明确不覆盖 TeamHub 来源接纳、队列调度、真实 Computer Runtime、Web
和主动学习，不能把这类结果叫作完整生产 E2E。
来源：[memory benchmark 说明](../reference-projects/linger/apps/node-services/scripts/memory-benchmark/README.md)。

Codey 应增加少量稳定的真实行为样本，比较现有词法基线、全历史索引和后续候选策略；固定任务和模型，
分别看来源正确率、旧信息更新、无答案拒答、召回、延迟、tokens 和额外请求。
现有契约门继续覆盖权限与恢复，模型质量验收单独记录。
Linger README 的 LoCoMo 95% 和本地推理速度是上游披露，本次没有复现，不能当作 Codey 改造后的预期收益。

落点：[公共事件](../codey/app/event_payloads.py)、[前端协议](../frontend/src/domain/protocol.ts)、
[机器契约门](../tools/machine_contract_gate.py)。

**后续可以研究的两件事，有明确边界。**

- 反馈学习：Linger 的 dreaming 把具体反馈先作为 clue，再形成可版本化规则，未回复不算正向或负向反馈。
  值得学“具体行为 → 对应反馈 → 可修正规则”的链路，而不是照搬 nightly model call。
  Codey 先复用用户显式提案和确认；普通回复不自动写成事实或长期偏好。
  来源：[dreaming 实现](../reference-projects/linger/apps/node-services/src/memory/proactive-dreaming.ts)。
- 后台委派：Linger 区分任务运行、等待审批、回报和取消，聊天可以继续，值得学习状态投射。
  Codey 可先服务于已授权的只读研究和独立工作区任务；同一项目仍保留单 Writer。
  不因参考项目支持委派就默认增加 Agent 数量、外部消息发送或主动联网。
  来源：[任务委派](../reference-projects/linger/docs/task-delegation.md)。

**不建议直接搬的部分。** 多服务部署与 PostgreSQL 会提高 Codey 的安装和维护负担；
全局桌面操作与连接应用扩大当前产品范围；自带大模型下载和推理引擎不属于网页 AI 工作台的必要闭环；
每五次运行抽取、六小时后整理和夜间自学习引入额外模型调用，也不符合 Ghost 默认成本目标。
Linger 部分 Runtime 的权限审查策略也不能覆盖 Codey 的统一授权与完成证明。

源码复用还有实际限制：Linger 的 [LICENSE](../reference-projects/linger/LICENSE) 是带托管服务和前端标识
附加条件的修改版 Apache 2.0，Codey 当前是 GPL-2.0-only，不能按标准 Apache 2.0 项目直接复制代码或素材。
本报告建议理解机制后独立实现；如需实际复用，应先核对具体文件与两边许可兼容性。

**实施顺序与本次验证。** 建议第一批只做“旧经历仍可检索”，第二批做显式偏好替代；
有真实争用测量后再做模型资源优先级。上下文索引、诊断和事件单源分别小步推进，不捆绑成内核重写。
自动推断、主动提醒和多 Agent 暂留研究，不作为以上改进的前置条件。

本次已确认 clone 成功、remote 正确、上游工作树干净、参考目录被忽略，并完成上述 201 条经历的后端探针。
临时库在探针结束后自动删除，没有使用用户状态库。没有修改生产代码，也没有据此宣称两边测试全绿。
文中的建议均为后续工作，尚未实施。
