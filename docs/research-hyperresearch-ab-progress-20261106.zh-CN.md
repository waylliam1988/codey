# Research 实验续跑检查点

本文件记录进行中状态，不是采用结论。执行顺序固定为 **2A → 2B → ③ → 4A/4B**。
没有正式新题和新题复核的净收益、机制触发与成本证据，生产保持不变。

## 最近的有效运行

- 源码提交：`ec29f5f8`；此前 `f325820e`、`cad44f9f`、`fcce55cf` 为分批实验基础设施提交。
- 运行工具：`tests/manual/research_live_web_matrix.py`。
- 当前目录：`.e2e-artifacts/research-live-web-2a-dev-json-frozen-v4/`。
- 当前命令：`python -m tests.manual.research_live_web_matrix run --root .e2e-artifacts/research-live-web-2a-dev-json-frozen-v4`。
- 工具会话：`24762`，应先轮询，**不可另起第二批推理**。
- 当前已完成：四个开发题两臂共 8/8 成功，工具会话 `24762` 已结束。
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
