# Research 合同收敛：分批实现与实机验证

本轮结合 OpenScience 对照与第一性原理复查实施。约束：沿用同一 kernel、原计划与结算回执、原 KnowledgeStore/effect、唯一冻结报告；保存不等于 answered。内容验收未通过，不推广自主检索和完整 WAL，不强制迁移新 strict 任务。

## 第一批：当前冻结合同贯穿提示、工具与反馈

- 从同一 TurnSnapshot 判断局部模式；首轮、native 协议修复、完成修复、输出上限续行均使用局部提交路径。
- 局部提示不再带全局 coverage、编码测试和启发式领域方法；普通全局模式保留。
- save_finding 成功只回传事实，不硬编码全局 done；候选仍 unreviewed。
- “转移数据”不再触发临床指导；局部计数缺口的正文与 allowed_actions 一致。
- 恢复沿冻结请求，不改历史请求正文或旧协议定义。

验证：79 项相关 pytest 通过（local contract、answer focus、local scope、relation targets、deterministic completion、read projection），包含已有进程中断恢复。另批提示所有权、请求投影及 findings 测试已通过；新测试首次两处构造参数错误已修正。mypy、pyrefly、ty 的 Python 3.11 检查及改动文件 ruff 通过。

实机：`.e2e-artifacts/research-contract-convergence-20261101/batch1-clean-contract/`。原 minimal 五题、74/75/76 seed、原材料顺序、原评分文件、固定模型可见时钟；未切换模型或采样参数。matrix.json 保存逐项人工四维评分、原始响应与请求定位。

| 原始完整问题 | 内容通过 | 观察 |
| --- | --- | --- |
| 单写者及原因 | 3/3 | 每次正确保存后立即选择 |
| 只读完整条件 | 3/3 | 版本、三条件与 OR 保留 |
| checkpoint 停止及原因 | 2/3 | seed 74 漏 end mark 边界 |
| checkpoint 记录与继续 | 0/3 | 两个所问事实正确，但主动附加错误停止边界 |
| 缺直接证据 | 0/3 | 两次最终 unresolved 前曾错误 answered；一次重复读取后 no_progress |

共 8/15 内容通过。done 仍不能代表内容正确。缺口评分保留“首次错误肯定”与“最终结束”差别，不能以父 gate 最终拒绝来算模型答对。清理合同后，直接证据四题没有重复保存或错误空提交；这只是本次样本观察，不能据此证明因果贡献。原固定 minimal 为 7/15，后端相同 seed 也非严格确定，不能把一题差异解释为稳定提升。

下一批只在同一固定证据层继续：缩小所问关系，保留完整原文与原整题标准；之后独立试验显式的新 finding 与局部选择合并提交。两者都不降低内容门槛。
