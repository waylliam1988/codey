# Research 审计诊断实施进度

目标：审计故障后继续 research，但在返回值、正式 trace、operation payload 和日志中区分失败、跳过及正常执行。不改变完成门槛、搜索或 followup 策略。

## 批次与验证

1. 结构化 review/ledger 状态；诊断贯穿返回值、trace 与 operation payload；保持 accepted audit 复用，legacy 区分 initial/final，每次 run 独立收集。已实现并通过首轮验证。
2. 按依赖隔离 snapshot/findings/gaps/source trust/brief；补充依赖跳过、空结果、异常及独立投影测试。已实现。
3. 扩展相关测试（不运行全量 pytest）、Ruff、类型检查，核对全部目标并更新此文档。已完成；全树既有类型错误的限制与首轮失败根因见下文。

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
- 全生产树 mypy/Pyrefly/ty 已运行，存在其他模块既有错误；已对修改前 HEAD 隔离副本做同环境对照（结果见最终验证），不将全树失败表述为通过。
- 新增真实 RunTraceStore JSON 落盘与 operation payload 透传测试；accepted 异常只审计一次，legacy 首轮失败后最终恢复仍保留两阶段记录。

## 第二批实现与失败分析

- snapshot、findings、planner gaps、source trust、brief 各有异常边界；错误写堆栈，依赖跳过给原因，正常空集合保留成功状态。
- source trust 在 record 存在时独立执行，包括 review 失败时；gaps 失败不阻止 brief。
- 覆盖每个阶段异常、snapshot/brief 返回 None、trace 转换异常、真实 ledger 缺失/损坏/恢复，以及 outcome 约束。
- 现有 A/B `no_proof_audit` 用旧 `None` 返回值替换私有方法，导致 AttributeError；根因是测试辅助代码未同步结构化返回契约，已更新辅助代码，保留禁用审计的原实验含义。
- 旧 trace fixture 不携带新字段，被投影额外加空列表导致两个精确比较失败；已调整为仅当输入包含诊断字段时投影。新 pipeline 始终携带诊断，真实落盘和 operation 测试验证不会丢失；历史 golden 无需更新。
- 扩展首轮 265 passed / 6 failed（另 23 subtests）；三项为以上契约/兼容问题，三项为冷恢复失败。冷恢复三项隔离重跑通过；按原顺序重跑前九个测试模块 148 passed、23 subtests，无修改恢复生产代码。随后通过临时副本的源码版本切换实验复现了精确的三个失败边界，根因见下文；最终完整相关组合已保留 JUnit 报告。
- 修正后的 trace、A/B 和核心诊断组合：116 passed。
- `python -m ruff check .` 与 `git diff --check` 通过。
- 四个修改生产模块：mypy（follow-imports=silent）、Pyrefly、ty 的 Python 3.11 检查均通过。

第一批提交：`bc80d027`。

## 最终验证（2026-11-07）

- 第二批提交：`161a360f`。
- 最终一次相关测试组合覆盖 20 个模块：**314 passed，23 subtests passed**，用时 110.41 秒。未运行全量 pytest。
- 覆盖 audit diagnostics/reuse/pipeline、proof quality、evidence ledger、query planner、review finding、brief、source trust、runtime observations、deterministic completion、whole kernel、completion gate、strict ledger、hybrid projection、continuation、question ownership、run trace、trace golden、branch boundaries。
- 最终 JUnit：`.e2e-artifacts/audit-diagnostics-validation/pytest.xml`。
- 对首轮三项冷恢复失败，另做不同父子进程 `PYTHONHASHSEED` 的 1→2、2→1、3→4、4→3 四组真实中断/恢复检查，均完成 `done`。结果保存在同目录 `cold-restart-seeds.json`。隔离重跑、同序重跑和最终组合均通过。进一步根因验证如下。
- 全仓 `python -m ruff check .` 通过。
- 修改前基线为 `64ef34c4`；用临时目录导出该版本的 codey、tools、pyproject.toml，在相同工具/环境下运行全树 Python 3.11 检查，并逐条比较错误位置和消息。

| 检查 | 修改前 | 修改后 | 本次新增 |
|---|---:|---:|---:|
| mypy | 16 errors | 16 errors | 0 |
| Pyrefly | 29 errors | 29 errors | 0 |
| ty | 27 diagnostics | 27 diagnostics | 0 |

全树类型检查依然失败，错误位于未改动的 attachments、kernel、provider 等模块；没有通过忽略规则或基线豁免掩盖。比较证据与原始输出位于 `.e2e-artifacts/audit-diagnostics-validation/typecheck-comparison.json`、`baseline-*.txt` 和 `current-*.txt`。

修改的四个生产文件单独检查均通过：

```powershell
python -m mypy codey/research/audit_result.py codey/research/pipeline.py codey/runs/trace_research.py codey/operations/research_flow.py --follow-imports=silent --python-version 3.11
pyrefly check codey/research/audit_result.py codey/research/pipeline.py codey/runs/trace_research.py codey/operations/research_flow.py --python-version 3.11
ty check codey/research/audit_result.py codey/research/pipeline.py codey/runs/trace_research.py codey/operations/research_flow.py --python-version 3.11
```

## 逐项核对

| 要求 | 当前证据 |
|---|---|
| 执行状态与审计质量分离 | negative proof quality 测试：执行 ok，review.ok=False |
| 无 record、异常和无效返回可区分 | skipped、proof_review_failed、invalid_proof_review_result 测试 |
| ledger 原因保留，不额外读取 accepted ledger | snapshot 原因、真实缺失/损坏/写入恢复、accepted 不调用 load 的测试 |
| 独立投影隔离且按依赖跳过 | 五阶段异常矩阵、None 投影、无 review source trust、trace 转换异常 |
| 空 findings 不等于失败 | 空 findings 仍执行 gaps/brief 的测试 |
| 返回、trace、operation 完整透传且有界 | 真正 RunTraceStore 落盘、research_payload、非法字段过滤、长度/行数限制测试 |
| accepted 失败不重试、legacy 分阶段、run 不串记录 | reuse 测试、首轮失败最终恢复、重复 run 测试 |
| 日志可定位故障 | caplog 验证异常类别及堆栈；对外 payload 无异常消息 |
| 完成门槛、搜索与 followup 保持原策略 | pipeline、whole kernel、completion gate、strict ledger、continuation、branch boundary 回归全部通过 |

本次没有改动完成门槛、搜索策略或 followup；额外诊断仅用于观测。投影在前置失败时继续执行独立阶段是明确预期的行为调整。

## 冷恢复失败根因与受控复现

首轮跨进程测试尚在运行时，代理修改了 `codey/research/pipeline.py`（为 Pyrefly 补上回调类型），使运行中的测试主进程与后启动的子进程处于不同源码版本。

`codey/operations/provider_receipts.py::runtime_implementation_id()` 对整个 codey Python 源码计算指纹，并用 `lru_cache(maxsize=1)` 按进程缓存。子进程保存新源码指纹后，旧主进程恢复时仍使用先前缓存的指纹；`kernel_context.py::execute_tools()` 因 implementation 不兼容拒绝执行。这是预期的安全恢复边界，不应修改生产代码绕过。

为验证根因，在独立临时目录导出第二批提交的 codey/tests/tools/pyproject.toml，先预热父进程源码指纹，再仅在该副本的 pipeline.py 追加无行为影响的注释，然后启动子进程中断并由旧父进程恢复。结果：

| 中断边界 | 受控版本不一致实验 |
|---|---|
| provider_request_ready | ResearchContinuationError：captured tool implementation is unavailable or incompatible |
| provider_response_ready | 同上 |
| tool_effect_pending | 同上 |
| kernel_settled | done |

该结果精确解释首轮的三个早期失败与后续边界通过；没有修改主工作树做此实验。复现输出位于 `.e2e-artifacts/audit-diagnostics-validation/implementation-transition-repro.txt`。

修正方法是停止在跨进程测试运行期间修改生产源码，并用稳定版本重跑；最终 314 项组合通过，未降低任何恢复校验。首轮另外三项失败分别由旧测试辅助返回契约和历史 trace 空字段兼容性引起，已在第二批修正。
