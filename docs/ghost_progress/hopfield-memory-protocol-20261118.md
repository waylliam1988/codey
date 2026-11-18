# Hopfield 记忆恢复实验：运行前冻结协议

- 实验：`ghost-hopfield-annotated-pilot-v1`
- 负责人：Codex；日期：2026-11-18
- 范围：直接在现有 master 保存实验，无 worktree，无生产修改。
- 状态：协议冻结；运行结果另记 `hopfield-memory-pilot-20261118.md`。
- 生产合并结论：`EXPERIMENT_ONLY`。

## 假设与成功条件

在相同经历、提供的语义表示、时间先验和上下文上限下，现代连续 Hopfield 能否比单步向量检索更准确恢复经历，并帮助同一个主模型适应用户变化？

仅当 live 完整矩阵同时满足以下条件，才记录为该小型实验的正向信号：Hopfield 比 cosine 至少多正确 4 格；四类任务均无退步；优于 latest_rule；所有组删除/过期/跨用户泄漏均为零。否则记录无收益或负结果；任何基础设施失败记 INCOMPLETE，不重试。不根据测试结果改参数、换题、换评分或删失败格。即使通过，也不批准生产实现。

## 样本和对照

四个合成用户，偏好分别以简短、详细、先计划、直接行动为起点，成对反转。每人 11 个快照：换说法/不完整表示 2 个、研究与改代码情境 2 个、偏好纠正 0–4 次 5 个、过期/删除 2 个；共 44 查询 × 6 组 = 264 逻辑格。四个用户是聚类样本，不能当作 44 个独立真实用户。

六组：

1. `lexical`：调用现有生产 `score_observation` 和 tokenizer，固定评估时间；保留原词汇重合和时间加权。辅助参照，不宣称完整生产检索路径或相同编码。
2. `cosine`：共享归一化向量，按 `8 * Xq + log_prior` 排序；也是同先验下单步 attention 的排序。主要对照。
3. `hebbian`：`W = XᵀX / n`，6 次带原查询锚点的线性传播与归一化，再用同一排序。是简单外积关联对照，不冒充现有 Ghost 图的完整生产实现或经典二值 Hopfield。
4. `fixed_state`：冻结实际校准前缀中的分情境偏好原话，以同一向量检索恢复；后续纠正不进入状态，删除/过期规则照常生效。是冻结偏好原型，不是故意指定错误答案。
5. `latest_rule`：匹配当前给定 tags 的明确反馈，按时间新旧排序；不看 gold。
6. `hopfield`：现代连续更新 `q_next = Xᵀ softmax(8 Xq + log_prior)`，6 次；不对更新状态重新归一化，最终用同一打分排序。

所有非词汇组使用相同的 192 维固定符号投影：人工标注项目、主题、情境概念，情境权重 2，其余 1；观察到的偏好值权重 0.6，仅写入记忆，查询无预期偏好值。固定 SHAKE256 特征投影，不拟合 gold，不调用 embedding，不加载本地 HF。`log_prior = -age_days / 30`。参数运行前固定，无测试集调参。

不完整线索题的自然语言仍能辨别研究情境，但给定表示故意缺情境标签。这是条件表示实验，不能证明自然语言自动编码能力；规则组在这里同样受到缺标签限制。

## 共同输入、预算和生命周期

每组获得同一用户截至查询时刻的全部候选经历（fixed_state 依其定义冻结前缀），选择前统一过滤未来、跨 scope、已删除和到期记录；每个快照重新建索引。每组最多渲染 2 条原话、完整历史块最多 768 字符，包括标题和分隔；不截断语义，不填充。模型看到相同模板、当前问题和选中原话，不看到 arm、gold、预期动作或选择器打分。

统一过滤后重建只证明索引不泄漏无效原文，不证明网络内参数遗忘、生产删除调度或底层语言模型训练。所有更改留在 tests/manual 和实验文档中。

## 实机协议

本地端点 `/v1/chat/completions`，运行前后 `/v1/models` 核对模型标识；单一已加载 Gemma 模型，temperature=0，输出最多 64 token，strict JSON schema 恰含一个 action。主模型只生成一次动作决策，Ghost 新增生成请求为 0。无二次改写、总结或模型裁判；不完整回复、HTTP、超时、模型身份变化立即停止，保留原始证据，禁止自动重试。首个回复不符合 schema 则停止；之后完整但格式错误的回复记行为失败，不剥 fence、不修复。

逐查询轮换 arm 顺序。**字节完全相同的请求复用同一个实机观察**，避免对相同输入重复生成；逻辑格、物理请求、复用数和来源 hash 分列。因此比较是确定性单次观察，没有温度复测或随机性置信区间。全部原始请求/回复仅留本地 `.e2e-artifacts`。

这是隔离 harness 的直接传输，未经过生产 runtime/builder/admission；不作为上线证据。该实验衡量的是模型输出的结构化行为选择，不是实际执行代码/研究任务的端到端成功率。

## 指标和停止条件

- 检索：top1、recall@2（分母是全部标注相关经历数）、hit@2、冲突偏好数、无效经历暴露；无相关经历时 top1 以是否正确空选计分，另列生命周期表现。
- 行为：严格解析动作是否正确、按 family 分列、Hopfield 对每个基线的成对胜负；不使用模型评分。
- 适应：0–4 次明确纠正的曲线；最早从第几次起至第 4 次始终正确，未适应记 null。
- 成本：实际历史字符数、服务 usage、模型延迟；检索另测 64/512/2048 条、预热一次、25 次查询的 p50/p95，建索引单列。扩容夹具重复已有 key，只测成本，不测语义容量。
- 内存：NumPy 数组字节、tracemalloc 建索引峰值、前后进程 RSS 分列；不把 Python 堆峰值冒充总内存。不是 TB 级训练实验。
- 数学/信息边界测试通过、源文件和 fixture/config hash 冻结后才运行离线及 live。缺失格不外推；完成预定矩阵即停止，不择优加跑。

## 复现入口

```powershell
python -m pytest tests/test_ghost_hopfield_experiment.py -q
python -m ruff check tests/manual/ghost_memory_routing/hopfield_*.py tests/test_ghost_hopfield_experiment.py
$run = '.e2e-artifacts/ghost-hopfield-pilot-20261118/run-v1'
python -m tests.manual.ghost_memory_routing.hopfield_experiment --output $run
python -m tests.manual.ghost_memory_routing.hopfield_experiment --output $run --offline
python -m tests.manual.ghost_memory_routing.hopfield_experiment --output $run --live
python -m tests.manual.ghost_memory_routing.hopfield_experiment --output $run --report tests/manual/ghost_memory_routing/reports/hopfield-memory-pilot-20261118.json
```

每次复跑使用新目录，不覆盖先前证据。冻结后改脚本需另建协议版本，不能无声续跑。
