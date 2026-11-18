# Ghost Hopfield 第二轮：运行前协议

- 实验：`ghost-hopfield-v2`；负责人：Codex；日期：2026-11-18。
- 用户授权：继续实验，争取得到更严谨的结论；保持不改生产、不复制 worktree。
- 状态：协议冻结后开发集选参，再运行未参与选参的测试集和固定实机子集。
- 生产合并结论：`EXPERIMENT_ONLY`。结果另记 `hopfield-confirmatory-result-20261118.md`。

## 要回答的问题

在共同输入、资格过滤、相关性门、空选规则、预算和输出方式下，基础现代连续 Hopfield 或保留查询的变体，能否提高经历恢复，并帮助单个已加载主模型依据正确经历选择响应方式？

这是未训练投影的机制比较，不是所有现代 Hopfield 架构的终审，不是 Nezha 既有实验的复现，不证明自动语义理解、真实用户长期收益或 TB 训练效果。

## 独立样本与表示

开发集 24 条独立生成经历流；测试集 96 条不同随机历史，均由 split/name 的 SHA256 seed 决定。每条经历流随机组合六种情境的主题/阶段/地点/对象、偏好、反馈次数、时间、少数例外与干扰；不是只置换动作标签。每流 14 查询：不完整线索和换说法/噪声 2、同用户不同情境 2、明确偏好改变 0–4 次 5、过期前后/删除前后/未知情境 5。共 1344 测试查询。

两条表示线各用 13/31/59 三个固定投影种子：

- `concept`：提供规范化概念，固定 256 维随机符号投影；观察值权重 0.6，查询不含预期动作。是隔离算法差异的 oracle 控制。
- `text`：直接从原始文本取 2/3 字符 gram，hash 到 256 维，TF-IDF 的 IDF 仅由开发集经历文本拟合。换说法字典仅用于生成查询，编码器不调用它，不读取规范标签或动作字段。此线是自动文本特征，**不冒充预训练语义 embedding**。

当前项目及请求中明示的情境名称为共同可见约束；未明示情境时不按隐含 gold 情境硬过滤。注入噪声题的真实目标由生成过程定义。所有测试 seed/查询及实机样本 ID 在选参前固定。契约测试可核查 fixture/gold 一致性，但不能根据测试算法成绩改题或选参。

## 八个对照与共同前处理

顺序为 `lexical / cosine / linear / fixed / latest / ghost_adapter / hopfield / anchored`。

- lexical：真实生产 tokenizer/score_observation，评估时间固定；是辅助评分参照，不宣称完整生产检索路径。
- cosine：原始查询打分 `beta Xq + prior`。
- linear：外积矩阵 `XᵀX/n`，带原查询锚点的线性传播与归一化。
- fixed：只使用实际校准前缀；资格/相关性过滤与其他组一致。
- latest：共同候选池中最新反馈；无额外 oracle 情境匹配能力。
- ghost_adapter：在独立临时 SQLite 真正调用生产 GhostHebbianStore.reinforce_candidate、明确替代、权重更新、共激活边、Affinity 同步/提示、directive 冲突处理与排序；每个新历史冷重开并 replay 校验。将条件情境放入 conflict_key、查询相关性过滤、把节点映射回原经历是**实验适配层**，不是生产自动提取、typed renderer 或完整 runtime 已有的能力。输入是已结构化/已接受的合成反馈，不测试接受过程的语义正确性。
- hopfield：`q_next = Xᵀ softmax(beta Xq + prior)`，不重新归一化；对应能量的下降有独立数学检查。
- anchored：`q_next = 0.25 q_original + 0.75 Hopfield(q)`。单列工程变体，不冒充原始能量更新。

共同先过滤未来、其他用户/项目、已删除和到期事件，以及当前请求明确限定的情境。然后按**原始查询**与记忆相似度应用同一阈值，阈值仅在开发集标定，所有组共享；低于阈值的候选不送入任何网络。候选池空则所有组空选。Ghost 可另外依其真实 competing-value 规则拒选，作为状态机制差异记录。

`prior=-age_days/45`；最多 2 条原话，总历史块最多 900 字符；不截断否定/情境，不给空选组填充。删除/过期题存在真实 before/after；当前情境明示，因此不能让“只有规则有资格空选”决定结果。共同过滤后重建不宣称网络参数遗忘；真实 store 删除/reopen/replay 另有隔离 probe。

## 开发集调参与锁定

先冻结全部源文件/fixture/config 的 hash 和生产树 hash。开发集用于 text IDF、共同相关性阈值、各 arm 参数选择；测试集不参与。

- 共同阈值候选：0、.15、.30、.45、.60、.75、.90、1.01；按开发集候选情境适用性的 balanced accuracy 选，平局用较低阈值。
- beta：2、8、32；linear/hopfield/anchored 迭代：1、3、6；cosine/fixed 无迭代参数。lexical/latest/Ghost 采用各自固定规则，无学习率调参。
- arm 参数按开发集 hit@2、precision@2、top1 依次选择；平局先少迭代，再小 beta。
- 每条表示线仅从 cosine/linear/latest 中选一个开发集最强的简单主基线，固定后才做测试比较。
- `development.json` 与独立 SHA lock 保存完整网格及选择；后续阶段校验 lock，不许按测试或 live 结果换配置。所有固定源文件全程保持冻结，修实现错误需另起版本，旧证据保留。

## 指标、统计与判定

离线主指标是恢复当前适用偏好的相关经历 hit@2；无相关经历时必须空选。另列 top1、precision@2、标准 recall@2、错误情境、无效原文暴露、0–4 次明确纠正曲线。反复满意反馈以稳定多数为生成偏好；明确“以后请”从首次纠正开始改变，相关源 ID 必须对应当前偏好。

所有种子与同流查询先聚合到经历流，再做配对 cluster bootstrap（10,000 次）。两个 Hopfield 变体 × 两条表示线共四个预注册主比较，使用 **98.75% 双侧区间**控制该组比较的 Bonferroni 名义错误率。其他对照/分组是解释性分析，不另造显著性结论。bootstrap 是对该合成生成器的近似区间，不是对真实用户总体的保证。

最低有意义收益事先定为 5 个百分点。点估计至少 +5pp、区间下界大于 0、各 family 无超过 5pp 退步且暴露为零，才是本任务上的正向信号。上界低于 +5pp 时，只能写“本生成器和配置下排除 ≥5pp 收益”；区间跨越该收益门时写证据不足。无显著差异不写“等价”。即使有收益也不进入生产。

## 实机消费与停止条件

固定测试流编号：2/9/14/23/31/38/46/55/63/71/82/91；每流 partial/noisy/context-a/context-b/drift-1/drift-4/expiry/deletion 共 8 查询。两条表示线使用 seed59，八个 arm 外加无历史 off；共 **1728 逻辑格**，与测试成绩无关的预定子集。

目标为当前端口 5001 已加载的 Gemma4-12B Q4 配置；调用前后 GET models 核对标识。主模型每次只生成一个 JSON `{action,source}`，source enum 只包含实际选中原话 ID 和 none；不包含 gold。只有动作符合当前真实偏好，且引用了正确相关经历，才算 grounded 行为成功；无适用经历必须 ask_user/none。碰巧猜对动作不能通过。另列 action-only 与 source 脱离的诊断。

temperature=0、最多 96 token、timeout120 秒；每个物理请求只试一次。完全相同请求 bytes 复用同一次观察，物理数/复用数/逻辑数分列。Ghost 额外生成请求始终为零，无 HF、无 embedding API、无第二次改写或模型裁判。模型只选响应方式和依据，不实际执行研究/编码，不能宣称端到端任务收益。实机 bootstrap 的独立簇只有 12 条，不是 1728。

HTTP、超时、截断、格式错误、模型身份变化：保留原始证据，停止，不重试、不修 scorer、不把基础设施故障算阴性。矩阵完成即停止，不因结果欠佳追加试验。用户明确恢复后才能补缺失格并保留故障说明。

## 开销与证据

另测 64/512/2048/8192 条来自独立生成历史的 key，非重复一个模式。每 arm 冷建编码缓存/索引一次，查询预热一次再 25 次计时，建索引与查询/排序分列；报告数组、编码缓存、tracemalloc、RSS。只比较四个向量机制的实现开销，不宣称存储容量、最佳实现成本或生产 Ghost 全路径延迟。

原始逐格、请求/回复和临时证据只在 `.e2e-artifacts/ghost-hopfield-confirmatory-20261118/`；仓库保存源码、脱敏汇总、阴性结果和复现说明。生产文件 hash 前后核对；保持原工作区，不纳入既有用户改动。

```powershell
python -m pytest tests/test_ghost_hopfield_v2.py -q
$run = '.e2e-artifacts/ghost-hopfield-confirmatory-20261118/run-v2'
python -m tests.manual.ghost_memory_routing.hopfield_v2_study freeze --output $run
python -m tests.manual.ghost_memory_routing.hopfield_v2_study develop --output $run
python -m tests.manual.ghost_memory_routing.hopfield_v2_study test --output $run
python -m tests.manual.ghost_memory_routing.hopfield_v2_study benchmark --output $run
python -m tests.manual.ghost_memory_routing.hopfield_v2_live --output $run --live
python -m tests.manual.ghost_memory_routing.hopfield_v2_live --output $run --report <新的脱敏JSON路径>
```
