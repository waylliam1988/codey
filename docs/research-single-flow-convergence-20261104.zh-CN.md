# Research 单流程收敛（2026-11-04）

## 最终职责

Research 只有「查资料 → 读原文 → 回答 → 保存新发现」一条流程。
长文续读、查旧知识和沿引用追溯原始版本都在同一任务里完成，不增加复杂模式或自动升级。

第一批从 Research 工具清单、提示和执行入口移除旧知识修订。模型不再调用
`knowledge_commit`，不再承担 `expected_revision`、完整旧对象复制、修订确认和回执处理。
新候选主张、推论和问题统一由 `submit_research_answer.knowledge_updates` 随完整答案保存。
Research 的独立写入、链接、保存 finding/inference 和计划注册入口一并停止暴露和执行。

第二批把全部严格 Research 入口统一为完整答案提交，移除根据旧 required_checks、
局部计划和当前子题决定流程的选择。删除 `local_scope.py`、`plan_contract.py`、
`action_feedback.py`，及其局部工作展示、选择预算、重开子题、结果拼接和局部修复状态。
提交工具的唯一输入是完整 `markdown`、声明的 `state` 和可选 `knowledge_updates`。

## 保留的边界

- 查旧知识、读固定版本及读取来源／证据以获得当前任务的 `[eN]`。
- 原文续读、实际阅读窗口、稳定引用标签和有界上下文展示。
- 候选知识与答案的原子发布、幂等保存、权限、取消、预算与当前流程重启恢复。
- 其他调用者使用的共享知识库提交、版本冲突、历史版本及关系处理。
- 历史报告解码和共享报告编译；它们不再决定当前 Research 的执行路线。

已经冻结的旧局部分题请求不能直接解释成完整答案请求。恢复解码明确拒绝该格式，
提示重新发起 Research；不会把局部结果发布为整篇答案。已经保存的历史报告仍然可读。

## 失败归因与测试迁移

首轮扩大回归暴露的主要问题是测试仍在发送旧 `findings`／`finding`、`research_plan`、
`done.report_sections`，或检查旧报告字段。这些断言描述的行为已被本次需求取消，
因此迁移到完整答案契约，而不是恢复旧生产分支来迎合测试。

纯局部计划、选择、内联 finding 和子题修复测试随对应实现删除。原文复用、任务锚点、
实际 API 请求、证据窗口、原子保存和断进程恢复检查迁移至完整答案路径。
共享知识维护实验继续隔离在测试代码中，明确使用 Research 之外的维护上下文。
历史报告的格式、版本与编译负面检查继续保留。

生产层补充了执行入口拒绝独立写入、拒绝旧局部 scope 的保护；工具列表隐藏不足以代替执行保护。
完整答案工具也只属于 Research，不能因清理旧条件而出现在其他任务的工具契约里。

扩大到内核和持久化边界后，还发现历史 parity 回放脚本仍发送旧 `knowledge_write/done`。
当前脚本单独保存在 `tests/fixtures/kernel_parity/current_research_scripts.json`，保留搜索、
原文读取、来源搜索、重开来源等原有动作，改用完整答案提交及候选知识批次。
固定的历史 baseline 不变；逐项登记当前预期差异，已经恢复历史行为的预算用例取消差异登记。

安装包实测暴露了独立的 Windows 长路径生产缺陷：验证副本中的模块文件已成功复制，
但普通工作目录使 Python 无法导入超过 MAX_PATH 的模块。独立复现确认扩展路径工作目录
可以正确导入，因此修复发布验证子进程的工作目录，并加入真实子进程导入回归。
不通过缩短安装路径、跳过验证或放宽运行时清单来绕过错误。

## 验证记录

环境为 Windows、Python 3.12.8；类型检查显式指定 Python 3.11 目标。

- 最终 Research 定向组：`rg --files tests -g 'test_*research*.py'`，
  **1444 passed，6 subtests passed**，407.62 秒。
- 扩大共享边界：内核、知识库、权限、工具契约、发布、完成检查、回执、`tests/durable`
  及 golden parity，初轮 **2387 passed，5 skipped，83 subtests passed，1 failed**。
  唯一失败是已恢复历史行为的预算用例仍保留差异登记；删除多余登记。
- 修复后复测：完整 legacy parity、parity regressions、拒绝完成预算及断进程恢复、
  evidence 契约与运行时、真实 wheel／源码包、发布验证缓存、前端物化、运行时边界、
  fail-closed sweep 和类型检查 CI 配置，**882 passed**，230.36 秒。
- 发布与升级控制额外复测：**7 passed**，124.15 秒。
- Ruff 全树通过；mypy 检查 575 个生产文件通过；Pyrefly **0 errors**
  （现有 35 suppressed、79 warnings）；ty 通过。
- `read_runtime` 核验 715 个发布文件与运行时清单一致；`git diff --check` 通过。

未运行全量 pytest，未发起新的真实模型 A/B。以上结果证明已覆盖场景的契约、执行与
恢复行为通过，不证明模型解释质量提升，也不提前承诺所有普通任务的语义质量不退步。
