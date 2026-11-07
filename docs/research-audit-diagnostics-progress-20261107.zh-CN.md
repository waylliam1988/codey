# Research 审计诊断实施进度

目标：审计故障后继续 research，但在返回值、正式 trace、operation payload 和日志中区分失败、跳过及正常执行。不改变完成门槛、搜索或 followup 策略。

## 批次与验证

1. 结构化 review/ledger 状态；诊断贯穿返回值、trace 与 operation payload；保持 accepted audit 复用，legacy 区分 initial/final，每次 run 独立收集。已实现并通过首轮验证。
2. 按依赖隔离 snapshot/findings/gaps/source trust/brief；补充依赖跳过、空结果、异常及独立投影测试。已实现。
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

## 第二批实现与失败分析

- snapshot、findings、planner gaps、source trust、brief 各有异常边界；错误写堆栈，依赖跳过给原因，正常空集合保留成功状态。
- source trust 在 record 存在时独立执行，包括 review 失败时；gaps 失败不阻止 brief。
- 覆盖每个阶段异常、snapshot/brief 返回 None、trace 转换异常、真实 ledger 缺失/损坏/恢复，以及 outcome 约束。
- 现有 A/B `no_proof_audit` 用旧 `None` 返回值替换私有方法，导致 AttributeError；根因是测试辅助代码未同步结构化返回契约，已更新辅助代码，保留禁用审计的原实验含义。
- 旧 trace fixture 不携带新字段，被投影额外加空列表导致两个精确比较失败；已调整为仅当输入包含诊断字段时投影。新 pipeline 始终携带诊断，真实落盘和 operation 测试验证不会丢失；历史 golden 无需更新。
- 扩展首轮 265 passed / 6 failed（另 23 subtests）；三项为以上契约/兼容问题，三项为冷恢复失败。冷恢复三项隔离重跑通过；按原顺序重跑前九个测试模块 148 passed、23 subtests，无修改恢复生产代码。冷恢复失败尚无可复现根因，不把单次重跑当作已修复，最终再运行完整相关组合并保留 JUnit 报告。
- 修正后的 trace、A/B 和核心诊断组合：116 passed。
- `python -m ruff check .` 与 `git diff --check` 通过。
- 四个修改生产模块：mypy（follow-imports=silent）、Pyrefly、ty 的 Python 3.11 检查均通过。

第一批提交：`bc80d027`。
