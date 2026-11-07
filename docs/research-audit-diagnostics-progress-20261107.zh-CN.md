# Research 审计诊断实施进度

目标：审计故障后继续 research，但在返回值、正式 trace、operation payload 和日志中区分失败、跳过及正常执行。不改变完成门槛、搜索或 followup 策略。

## 批次与验证

1. 结构化 review/ledger 状态；诊断贯穿返回值、trace 与 operation payload；保持 accepted audit 复用，legacy 区分 initial/final，每次 run 独立收集。已实现并通过首轮验证。
2. 按依赖隔离 snapshot/findings/gaps/source trust/brief；补充依赖跳过、空结果、异常及独立投影测试。待实施。
3. 扩展相关测试（不运行全量 pytest）、Ruff、类型检查，核对全部目标并更新此文档。待实施。

## 契约

- `ok` 表示执行成功，与 `ResearchProofReview.ok` 的质量结论独立。
- `skipped`/`error` 无业务结果，具备稳定原因码；异常类别可对外输出，堆栈写日志。
- 正常空 findings 为成功；异常不得伪装为空 findings 或不合格 review。
- accepted 审计异常不重试；ledger 仅在原有需要读取的路径读取。
- 诊断有界，纯诊断，不驱动 planner、提示词或完成判断。

## 初始证据

- 工作树起始干净；pipeline 的 review、ledger 读取和投影异常存在静默路径。
- ledger 写入已有 `write_failed` 结构化结果，应保留。
- 配置要求 Python 3.11+，Ruff；CI 类型检查命令为 `python -m mypy codey`。
- 前次核查相关现有测试 3 项通过。

## 第一批验证

- `python -m pytest tests/test_research_audit_diagnostics.py tests/test_research_audit_reuse.py tests/test_research_pipeline.py -q`：30 passed。
- 修改文件 Ruff 通过；修改的 4 个生产模块 mypy（`--follow-imports=silent --python-version 3.11`）通过。
- 全生产树 mypy/Pyrefly/ty 已运行，存在其他模块既有错误；正在对修改前 HEAD 隔离副本做同环境对照，不将全树失败表述为通过。
- 新增真实 RunTraceStore JSON 落盘与 operation payload 透传测试；accepted 异常只审计一次，legacy 首轮失败后最终恢复仍保留两阶段记录。
