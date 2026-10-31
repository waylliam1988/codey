# Research 验收收敛与本地验证，2026-10-31

## 1. 历史回归的五项失败

用户要求先查明 `220 passed, 5 failed` 的原因，再调整同模型强制自审。
本项没有运行模型，没有修改历史原始记录。

| 失败 | 定位 | 修改 |
| --- | --- | --- |
| incomplete_done_frame_gets_specific_repair_without_executing | 测试过期：普通任务却期待旧版 Research 字段指导 | 保留不执行畸形调用的断言，检查当前普通 done 的 summary 修复指导 |
| manual_knowledge_refs_are_rejected_by_the_frozen_report_contract | 测试过期：先带入已移除的 summary，尚未到达待测 knowledge_refs 字段 | 使用当前 coverage 唯一报告输入，再验证 knowledge_refs 被拒绝 |
| headless_rejects_displayed_commit_then_completes_after_real_read_and_commit | **生产 schema 与领域对象不一致**：KnowledgeObject 默认 title=""，to_dict 合法输出空标题，但 mutation_schema 要求非空 | 工具 schema 允许空标题；原真实入口测试继续验证实际读取、提交与引用成功，其他必填内容仍非空 |
| canonical_split_preserves_valid_ledger | 测试过期：原文有 `2026`，引文擅自删除，过去依赖自动换引文 | 测试提供连续真实原文，生产仍拒绝伪造/拼接引文 |
| knowledge_write_split_preserves_save | 测试过期：URL 被当作充分证据，未提供显式 evidence | 提供原文、来源、主张及关系，生产仍不自动生成证据 |

本项只调整一处生产 schema 及三份旧测试；真实提交测试无需更改，即可覆盖空标题的生产修复。

验证：原同组 225 项全部通过（35.62 秒），Python socket 连接已禁用，无连接尝试；Ruff 与 git diff --check 通过。

后续验收方向：确定性校验约束来源、原文、精确版本、任务要求交代和发布回执；同模型正面自评不得提升为独立内容认证。执行完成和语义正确应明确区分。
