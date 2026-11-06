# Research 实验续跑检查点

本文件记录进行中状态，不是采用结论。执行顺序固定为 **2A → 2B → ③ → 4A/4B**。
没有正式新题和新题复核的净收益、机制触发与成本证据，生产保持不变。

## 最近的有效运行

- 当前正式冻结源码提交：`77838e44`；此前 `f325820e`、`cad44f9f`、`fcce55cf`、`ec29f5f8` 为分批实验基础设施提交。
- 运行工具：`tests/manual/research_live_web_matrix.py`。
- 开发目录：`.e2e-artifacts/research-live-web-2a-dev-json-frozen-v4/`，四题两臂 8/8 成功，工具会话 `24762` 已结束。
- 当前正式目录：`.e2e-artifacts/research-live-web-2a-formal-json-v1/`。
- 当前命令：`python -m tests.manual.research_live_web_matrix run --root .e2e-artifacts/research-live-web-2a-formal-json-v1`。
- 工具会话 `75913` 已结束；本合同依停止规则封存为 inconclusive，**不可用新源码继续本目录**。
- 正式计划 48 runs，实际 10 次尝试，9 发布、1 失败，全部保留。失败是 arthritis candidate 未读引用被拒绝后控制串循环。
- 找到生产 schema 正则不兼容：`(?:...)` 导致 KoboldCpp 1.117.1 丢弃整个 grammar。
  已将 `knowledge_updates._REF_PATTERN` 改为等价捕获组，真实编译成功且原失败 payload 三 seed 合法 JSON。
  artifact：`kobold-recovery-20261106/schema-compiler-probe.json`、`fixed-reference-schema-replays.json`。
- 下一步：提交此确定性 bug 修复；在修复后基线上重新开发与冻结新的 2A 合同，排除已运行正式题。
  生产修复提交 `a14172a8`；Mypy 全 597 文件、Pyrefly/Ty 均通过；新定向测试 74 项通过。
  后续增加了真实工具 schema 编译预检，失败拒绝推理；相关引用／入口测试 51 项通过。
- 当前修复后开发目录 `.e2e-artifacts/research-live-web-2a-dev-compiled-v1/`，源码提交 `2fe2be83`。
  正在运行 `python -m tests.manual.research_live_web_matrix run --root .e2e-artifacts/research-live-web-2a-dev-compiled-v1`，
  会话 **76882**，必须先轮询；不要修改 manual 或 commit HEAD，避免破坏当前冻结。
- 开发矩阵已完成 8/8；每次实际发送的 3 个 JSON tool schema 均有非空 GBNF 编译摘要。
- 正式新题目录 `.e2e-artifacts/research-live-web-2a-formal-compiled-v1/` 在第 5 次运行暂停：
  `pubmed_pancreatitis-1301-browser_after_connector` 读取 PMC 长文触发 `invalid read passage descriptors`，
  随后 `no_progress`。根因是 `fitting_full_read` 预览产生超过 32 个章节 descriptors，而恢复器有 32 条上限。
  已修复为超过上限直接保留原读取；该冻结目录因生产源码身份改变封存，不能续跑。
- 2A 已完成的四个 neuropathy 样本已做独立证据复核，四份严格成功均为 false；候选的浏览器发现更相关摘要，
  但未达到完整任务净收益门槛。评分保存在 formal 目录 `semantic-ratings.json`。
- 使用读取边界修复后的新源码重新冻结 `research-live-web-2a-formal-compiled-v2`，首个 baseline 运行重复打开同一 PubMed 结果，
  以 `no_progress` 结束且没有提交答案。按合同保留失败并停止，计划 48、实际 1，`decision.json` 判 `inconclusive`；不能据此采用或否定候选。
- 2B 正式矩阵 `research-live-web-2b-formal-v1` 已完成 32/32 个真实运行。候选 16 次中 14 次恢复公开全文、2 次无公开链接，
  所有运行均产生真实提交；尚未完成全量配对语义评分，因此 `decision.json` 判 `inconclusive`，生产不变。
- 新正式题池在 `research_live_web_matrix.py`：剔除已运行前三题，八个短主题首搜与四个自由搜索对照；
  共同协议仍为约束 JSON + none effort，增加真实 GBNF 编译确认。不要复用旧 frozen-v4 或 formal-json-v1 目录。
- 首题两臂 connector 都未返回命中，因此早停机制未触发；不能将组间差异计为机制收益。
  四份答案均从 UpToDate 导航/目录页推断治疗建议，严格成功皆否；独立复核保存为
  `semantic-ratings.json`，匿名材料为 `review-packets.json`。后续必须继续完成其余任务并更新评分。
- 2B 尚未运行；适配器草案放在 `.e2e-artifacts/research-next-candidate-drafts/fulltext_live.py`，
  不属于被冻结 manual 源码，尚未检查/采用。后续独立开发时再迁入实际实验目录。
- 共同条件：32k context、8192 reserve；生产工具调用实际 cap=4096；temperature=.3；
  constrained JSON；`enable_thinking=false` 与实验请求层 `reasoning_effort=none`；真实网络与真实 writer。
- `none` 不属于生产可选 effort，不能写进生产配置；仅在隔离 adapter 的 `ApiProvider` 构造时包裹
  `configure_request` 注入。入口测试覆盖这个边界。所有候选仍不在 `codey/`。

## 下一步

1. 先完成四题开发双臂。失败先查原始 wire/事件，不能静默重试或混算旧协议结果。
2. 正式冻结前完善每次 connector/browser 返回 URL 的观察记录，并核对共同请求控制在实际 wire 中可见。
3. 运行结束后才能修改 manual 脚本或提交，否则冻结检查会因 manual 全目录摘要或 HEAD 变化而拒绝继续。
4. 开发稳定才冻结 formal：12 个未用主题，seed 1301/1302，共 48 runs；交错两臂。
   `research_live_web_matrix.py` 已定义与开发分离的 formal/confirm 题池。
5. 逐题源文复核最终答案、引用、发布、成本；禁止用关键词、writer 自报或工具成功代替严格成功。
6. 正式满足至少 3 正向任务、至多 1 负向任务、严格成功率 +10pp、无新增严重或普通任务回归、
   token/时延增量均不超过 25%，才做 8 新题 × 2 新 seed（1401/1402）复核；之后才讨论最小生产改动。
7. 随后依次开发并独立冻结 2B、覆盖比较面的③（不是旧词项候选）、4A 与 4B。它们尚未完成本轮真实正式矩阵。
8. 更新主审计 `research-complete-branch-review-20261103.zh-CN.md`、本文件和结果记录，分批 commit。

## 不要重复的诊断

- plain 模式失败常是重复 channel/thought 控制串，不是普通答案长度超限；增加 reserve 没用，已恢复 8192。
- 仅 constrained JSON 也不稳定；v1 开发 candidate 搜索后仍截断。
- v2 在推理前因生产 effort 配置拒绝 `none` 停止；v3 因错误 patch 位置在推理前停止；均保留且不评分。
- 相同真实失败 payload 的有界重放保存在
  `.e2e-artifacts/kobold-recovery-20261106/protocol-replays.json`。
- 后端 `KoboldCpp 1.117.1`，模型 `Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`。
- 用既有 `D:/E backup/koboldcpp/Gemma4-12B-QAT/2.kcpps` 重启；启动 stdout/stderr、对应版本上游源码和
  实际 GGUF chat template 保存在 `.e2e-artifacts/kobold-recovery-20261106/`。
- launcher 和 server 的父子进程不等于两个独立模型。当前只应有一个 5001 listener。
- 因重叠开发批次造成过残余排队，已停止客户端并重启。现在使用 socket 锁和 idle/queue 检查，失败即停止。

## 验证

- 第一批 42 项定向测试通过；另有 answer/publication/source connectors 54 项通过；未跑全量 pytest。
- 之后入口注入回归加入后，本实验测试 5 项通过。
- 另补当前整篇真实提交与 kernel 测试 30 项通过（`test_research_whole_answer.py`、`test_research_whole_kernel.py`）。
- Ruff 通过；Mypy 全 `codey` 597 文件通过；Pyrefly 0 errors（既有 35 suppressed、81 warnings）；Ty 通过。
- 先前两项新测试失败来自 fixture 未包含生产 merge 标准化后的空 snippet，已修正测试，未改生产。
- 尚无确定性生产 bug 修复，也无候选合入生产。
