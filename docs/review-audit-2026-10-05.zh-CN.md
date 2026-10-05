# Codey 迭代审查收口（2026-10-05）

本轮完成 7 轮独立审查。最终全量 pytest 只运行一次；其后没有再改生产代码或测试代码。

## 轮次记录

| 轮次 | 扫描范围 | 新候选数 | 结果 |
| --- | --- | ---: | --- |
| A | 状态迁移、异常/fallback、生命周期、取消/超时、provider、HTTP、SSE、CLI、headless、恢复和冷启动 | 2 | 确认并修复 CAND-001；确认 provider 非 JSON 边界风险 |
| B | 弱断言、fixture 语义、失败分支、skip/xfail 依据、正式入口和外部探测 | 1 | 确认 UI gate 曾复制研究协议；改为只发送用户任务 |
| C | 导入关系、死代码、重复 loop、旧协议/兼容层、fallback 消费者和所有权边界 | 0 | 架构/冷启动锁通过，无可安全删除兼容层 |
| D | 等待、重复连接/发送、终态显示、SSE 重连、browser worker、外部资源隔离 | 2 | 真实 KoboldCpp `chat,coding,review,ghost` 通过；research 暴露搜索/模型稳定性风险 |
| E | 修复后的反向调用方审查和严格 mypy 边界 | 5 | 确认并修复 CAND-002～005；生产 research prompt 增加格式/恢复约束 |
| F | 调用关系和状态流二次扫描 | 0 | 无新可复现问题 |
| G | 测试/fixture/fallback/冷启动二次扫描 | 0 | 39 通过，无新可复现问题 |

## 候选问题台账

| 编号 | 文件/行 | 复现命令与失败输出 | 根因/是否 bug | 回归、修复和受影响范围 | 再扫描 |
| --- | --- | --- | --- | --- | --- |
| CAND-001 | `codey/app/task_submit.py:30-106` | `python -m pytest -q tests/test_task_submit.py::TaskSubmitTests::test_run_task_uses_captured_state_for_post_entry_cleanup`；旧实现因第二次 `get_state()` 抛出 `RuntimeError: accessor down after entry` | 生产代码；确认是 bug：已捕获的 state 被丢弃，cleanup accessor 异常会掩盖任务结果 | 先红后绿；复用初始化 state 执行 `kick_if_idle`。新增 `tests/test_task_submit.py:161`；受影响任务提交/HTTP/headless 回归通过 | 生产、测试、兼容层各重新扫描；F/G 再次确认 |
| CAND-002 | `codey/app/task_submit.py:173`、`codey/app/server.py:226-232,284,487` | `python -m pytest -q tests/test_mypy_shared_boundaries.py`；`Returning Any ... no-any-return` | 严格类型边界；确认是静态卫生 bug，不改变运行时语义 | 以显式边界变量注解收窄动态返回；9 个 mypy 边界测试通过 | 全树 mypy 0 错误 |
| CAND-003 | `codey/workspace/changes.py:132-986`、`codey/completion/contract.py:91-103` | 同上；`Returning Any ... Path/str/bool` | 动态导入边界未声明返回类型；确认是静态卫生 bug | 显式注解 snapshot/path/结果值和 completion ref；边界测试通过 | 全树 mypy/Ruff 通过 |
| CAND-004 | `codey/runtime/core/cancellation.py:41-49`、`codey/operations/task_loop.py:467-472` | 同上；`Returning Any ... int` | 跨模块常量/坐标 helper 在 skip-import 模式下退化为 Any；确认是静态卫生 bug | 对默认值和坐标使用显式局部类型；边界测试通过 | 全树 mypy 通过 |
| CAND-005 | `codey/operations/project_writer_phase.py:50,257`、`codey/research/evidence_ledger.py:45,216,581,1277,1488`、`codey/runs/trace.py:389` | 同上；`Returning Any ... tuple/bool/int/str/Path` | writer/evidence/trace 动态边界未收窄；确认是静态卫生 bug | 显式局部类型注解，保持所有权和运行时路径不变；边界及受影响测试通过 | 全树 mypy 372 文件 0 错误 |
| ENV-001 | `tests/test_operator_bootstrap_before_ui_requests.py` 3 项、`tests/test_sse_browser_dedup_reset_and_buffer_gap.py` 2 项 | 最终 `python -m pytest -q` 失败，`FileNotFoundError: [WinError 2]`，失败发生在 `subprocess.run(["node", ...])` 启动前 | 测试环境限制，不是生产 bug；Node.js 不在 PATH。没有改 skip/xfail 或测试断言 | 记录为剩余风险；Python/SSE 服务端等价回归通过 | 不纳入可复现生产候选 |
| CAND-006 | `codey/providers/local_openai.py:619-625`；HTTP 200 body 为 `not JSON` 后第二次返回合法 JSON | provider 边界；确认是有限重试缺口，不放宽 JSON 契约 | 新增失败回归后将非 JSON 纳入一次 `_RetryableResponseError` 重试；持续非 JSON 仍失败；4 项 provider 诊断测试通过 | provider、UI history、fallback 再扫描 |
| CAND-007 | `codey/operations/kernel_prompt.py:71-93`；12B 使用粗体章节、错误 note path，质量门拒绝 | 生产引导问题叠加模型能力边界；不是内核判定 bug | 生产 prompt 增加独立 `##` 章节、`[n]` 引用、反证模板、原样 note id 和恢复步骤；回归测试通过 | UI gate 只保留用户任务；实机 history 复核 |
| CAND-008 | `.e2e-artifacts/local-gate-research-final2`；DuckDuckGo `search_timeout` 后重复同一 `web_search`，终态 `no_progress` | 外部搜索/模型稳定性风险；没有生产代码证据支持修改完成门 | 记录为风险；无 JSONL parse/transport error，不增加无界重试 | research-only 复测和 provider history 留档 |

排除项包括：仅存在于测试数据字符串中的 `assert True`、已有明确 git/Node 环境依据的 skip、架构测试引用的历史 alias，以及有真实调用方的 provider/browser fallback。它们分别由 AST/架构锁、调用关系和定向行为测试证明，未删除兼容层。

## 最终闸门和原始统计

- `python -m mypy codey`：0 errors，372 source files checked。
- `python -m ruff check codey tests tools`：通过。
- `python -m compileall -q codey`：通过。
- `python -m pytest --collect-only -q`：7305 tests collected。
- `git diff --check`：通过。
- 受影响回归：359 passed，381 subtests passed。
- 唯一一次最终全量：`pytest -q`，7286 passed、14 skipped、5 failed、1497 subtests passed，488.84 秒。
- 最后一轮 G 和倒数第二轮 F 都没有新问题：G 覆盖 fixture/fallback/冷启动并通过 39 项回归；F 从调用方和状态流复核 CAND-001～005，未发现修复破坏入口或新增死接线。

剩余风险是 Node.js 缺失导致的浏览器 JavaScript 行为未在本机执行，以及真实搜索/provider/CDP、12B 报告质量和完整浏览器 E2E 的环境依赖；这些限制不被用作审查收敛证明。

审查实现提交：本轮代码修改尚未提交。文档更新完成并通过 diff 检查后才创建 commit/push。
