# Research 四批实施与验收记录

实施基线：`751c6f46`，2026-11-02。用户授权每批一个 commit，定向及相关扩展回测、Gemma4 12B 实机验证；不跑全量 pytest。原有未跟踪审计文档保留，不混入生产改动。

## 固定验证边界

使用已有 `tests/manual/research_framework_comparison.py`、透明 wire 代理和原 headless 入口。模型为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`；temperature=0.3、top_p=0.9、初始 seed=74，后续配对扩展到 74/75；thinking=false、输出至多 4096 token、上下文 32768。每个目录冻结 runtime/harness/material 指纹；矩阵运行期间不改生产源码。

原始数据在 `.e2e-artifacts/research-convergence-20261102/{baseline,batch-a,batch-b,batch-b-final,a-control-seed75,navigation-only}`。C/D 尚未实施。不是新研究运行时；这些目录仅保存隔离测评状态。复用历史冻结题，不能作为未见材料泛化证明；少量 seed 不证明稳定优于旧版。正文完成、内容正确、知识积累分别判断。

修改前两题：WAL 3 次请求、asyncio 2 次请求，均发布。WAL 将“只读打开替代条件”误解成“WAL 替代回滚日志”，漏掉三个 OR 条件，另有性能外推；asyncio 保留取消传播不受 return_exceptions 影响，但对子任务取消不连带取消其他任务的表达仍不够明确。不能把发布成功算内容全对。

## A：版本邻居与历史格式

- `KnowledgeRecall.neighbors`：入边严格匹配目标 revision；按 `(id,revision,relation)` 去重，沿用原权限和来源 head 策略。
- `restore_result_receipt`：按 out_/artifact_ 明确分发。旧格式先验证原 JSON 字节摘要、session/run 存储归属、effect/tool 身份和非截断，再走共同内容校验。新写仍用 JSON artifact。
- `KernelResult`：新写显式 schema_version=2，严格读取已知旧六字段、七字段；旧结果只补 research_report=None，不补成功证明。
- 保留 `751c6f46` 的个人/项目归档与历史材料通道；已有权限测试继续执行。

验证：新增兼容测试 13 项通过；扩展命令 83 项通过；另一个涉及原决策、回执和严格字段的命令 20 项通过。命令有重叠，不相加成独立用例总数。新测试最初误用不存在的 KnowledgeStore 上下文管理器，修正测试后通过；未为通过测试放宽生产权限。

实机：WAL/asyncio/RLock/长手册/跨来源共 5 例均交付，请求数分别 3/2/2/3/3。RLock 正确区分内部解锁与普通 release；长手册实际读到尾部，必要恢复关系完整。WAL 仍误解第二问，跨来源仍漏 24 小时 TTL（沿用旧评分，题目未直接问期限的局限保留）。本批证明确定性修复未阻断研究，不声称提升内容能力。一次使用错误 case 名 `long-manual` 被参数检查拒绝、未进入推理；按真实冻结 ID `long-recovery-manual` 补测，原记录保留。

## B：组合请求投影未通过验收，退出活动代码

实验实现了独立原问题、稳定 system 原则、真实 tool 原文、实际 payload 发送前计数与冻结、冻结输入恢复不再编码/裁剪，并阻止 configure_request/准入 gate 修改材料。新增 API/内核请求投影测试 9 项通过；相关组分别 51、66、57、32 项通过（重叠，不累加）。真实内核测试发现 new_chat 后计数误判 generation 失效，修正生产代码后通过。这些只证明结构边界，不证明内容收益。

首轮 `batch-b`：五例中四例发布，长手册连续三次不带 offset 读取开头后 no_progress。导航仍在请求中，含未读范围、末节标题和 offset=18416；不能归因为入口缺失。WAL 的只读 OR 条件恢复，性能边界仍有问题；asyncio 漏无论 return_exceptions 的取消传播条件；跨来源漏项目范围与 TTL。结构测试通过不能抵消此回退。

加入冻结输入不可被配置/准入改写的防护后，用独立指纹目录 `batch-b-final` 重复已排队的同五例。长手册该次交付，但耗六次请求；这与首轮三次请求失败并列保留，不能只报成功重跑。native call ID 等输入并非逐字固定，固定 seed 不等于确定性复现。

用户据结果要求缩小实验。保留 A (`13a756ea`)，B 尚未提交，不作为整体改进推广；D 暂缓。完整 B 源码、tracked.patch 和逐文件 SHA256 保存在 `.e2e-artifacts/research-convergence-20261102/shelved-b/`，不是运行时状态。活动代码回到 A 后，仅检验“由原回执派生的阅读范围/下一未读位置/有界目录在当前结果末尾展示”，不扩大窗口、不改变角色组织、提交合同或无进展保护。A 与最小修正版作冻结对照，再决定方向；C/D 不因既定四批计划而自动推进。

## 最小导航实验：未证明整体收益，已退出活动代码

函数范围仅为 `answer_context.read_result_navigation()`、`answer_context()`、`task_guidance.task_result_context()` 和 `KernelContext.deliver_results()` 的参数传递。导航由原阅读回执与 ledger 单向派生，没有新增可写状态。保留 A 的“工具回执指向当前材料工作视图”组织，因此短导航同时附在工具回执与材料工作视图末尾，提交指令仍在最后；没有把 B 的角色分层重新夹带进来，也没有重复展示原文。导航可用性与事实内容收益仍须实测。

离线验证：导航/修复定向命令 13 项通过，相关扩展命令 96 项通过（65 秒），ruff 通过。这两个 pytest 命令有重叠，不相加；没有全量 pytest。扩展范围包括整篇研究、长期知识复用、请求冻结、回执完整性与 A 的兼容测试。没有为了测试通过调整生产权限或放松验收。

实机配对范围为 WAL、asyncio、长手册各 74/75，RLock、跨来源各 74，共八例；A 对照也覆盖同样八例。人工逐条检查必要条件，独立保留发布结果与内容问题：

| 案例／seed | A 请求数／交付 | 最小导航版请求数／交付 | 内容观察 |
| --- | --- | --- | --- |
| 长手册 74/75 | 各 3／成功 | 各 3／成功 | 都直接续读 offset=18416，必要条件完整；与 A 持平 |
| WAL 74/75 | 各 3／成功 | 各 3／成功 | 导航版 74 恢复三项只读 OR，75 又误解为替代回滚日志；两例未完整守住性能证据边界 |
| asyncio 74 | 2／成功 | 3／成功 | 首次普通正文写对 regardless-return_exceptions，正式提交丢失；单个子任务取消的隔离关系仍不够明确 |
| asyncio 75 | 2／成功 | 5／失败 | 首次正文包含必要条件，后有伪工具标签与两次 length 截断；不存在可验收的最终提交 |
| RLock 74 | 2／成功 | 3／成功 | 两版内容正确，导航版额外进入一次协议修复 |
| 跨来源 74 | 3／成功 | 3／成功 | 项目范围等关系保留，均漏旧评分要求的 24h TTL；题目未直接问期限的评分局限保留 |

A 共 8/8 交付、21 次准入推理请求；导航版 7/8 交付、26 次。不能把导航版长文成功说成优于 A，也不能以 WAL 单个 seed 的改善抵消其他问题。这里不公布一个掩盖条件差异的总内容分。

asyncio/75 的最终停止诊断是 `local provider emitted finish_reason=length twice`，不是六分钟进程强杀；wire 另记录三次 `ConnectionAbortedError(10053)`。应将它记为真实部署上的生成／协议交付失败，并注明传输干扰，不能仅凭此证明导航展示导致失败。保持原 4096 输出上限、无进展与 provider 错误保护，没有解析执行正文里的伪工具调用。

两个 A 目录的字节级 runtime 指纹不同。已用已提交 A 源码和八个文件的 CRLF/LF 组合精确重建首次 A 指纹，确认差异仅为换行字节；证明在 `.e2e-artifacts/research-convergence-20261102/a-source-equivalence.json`。材料与 harness 指纹完全相同。native call ID 等仍可能变化，因此这些是冻结配置对照，不是逐字相同请求的确定性重放。

**处理结果：仅保留 A 的生产实现。** 导航版三个生产文件和一个测试文件、补丁及 SHA256 已完整保存到 `.e2e-artifacts/research-convergence-20261102/shelved-navigation-only/`；随后只恢复本实验的三个生产文件并移出本实验测试。恢复时运行源码、harness 和 HEAD 精确匹配 A 的 seed75 对照指纹。B 与导航实验均未作为已验收批次提交，C/D 未推进。随后按用户要求单独提交文档与 JSON 审计报告；该文档提交不改变 A 的生产实现，实验 JSON 中的 commit 仍表示被测版本。

逐例判断、材料与 wire 摘要、指纹和测试命令见 [导航实验记录](reports/research-navigation-experiment-20261102.json)。所有首轮失败与重跑成功并列保留，不用重跑覆盖失败。

## 下一步边界

当前更直接的候选是单独检验协议修复中的完整失败正文投影。`kernel_prompt._repair_prompt()` 仍仅把被拒响应的 hash 与前 600 字符 preview 放回请求；这不能覆盖长正文尾部条件。B、B-final 和最小导航版 asyncio/74 都出现了“首次正文包含关键条件，正式提交遗漏”的实际记录。但首次正文也可能有别的错误，不能原样保存就自动批准，更不能自动执行其中的工具标签。

后续若开展该实验，只改变原 provider 响应到修复请求的只读投影，检验完整响应可恢复、实际修复请求包含必要正文、最终提交是否保留关键内容三项；继续使用真实提交、原 gate 和发布事务。不要与角色重排、扩大阅读、D 的知识合同同时实施，也不提前承诺内容收益。
