# Codey 迭代审查收口（2026-10-05）

本轮完成 7 轮独立审查。最终全量 pytest 只运行一次；其后没有再改生产代码或测试代码。

## 轮次记录

| 轮次 | 扫描范围 | 新候选数 | 结果 |
| --- | --- | ---: | --- |
| A | 状态迁移、异常/fallback、生命周期、取消/超时、provider、HTTP、SSE、CLI、headless、恢复和冷启动 | 1 | 确认并修复 CAND-001 |
| B | 弱断言、fixture 语义、失败分支、skip/xfail 依据、正式入口和外部探测 | 0 | 未发现新可复现问题；Node 探测依据明确 |
| C | 导入关系、死代码、重复 loop、旧协议/兼容层、fallback 消费者和所有权边界 | 0 | 架构/冷启动锁通过，无可安全删除兼容层 |
| D | 等待、重复连接/发送、终态显示、SSE 重连、browser worker、外部资源隔离 | 0 | 40 通过、2 个 Node 环境跳过 |
| E | 修复后的反向调用方审查和严格 mypy 边界 | 4 | 确认并修复 CAND-002～005 |
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

排除项包括：仅存在于测试数据字符串中的 `assert True`、已有明确 git/Node 环境依据的 skip、架构测试引用的历史 alias，以及有真实调用方的 provider/browser fallback。它们分别由 AST/架构锁、调用关系和定向行为测试证明，未删除兼容层。

## 最终闸门和原始统计

- `python -m mypy codey`：0 errors，372 source files checked。
- `python -m ruff check codey tests tools`：通过。
- `python -m compileall -q codey`：通过。
- `python -m pytest --collect-only -q`：7290 tests collected。
- `git diff --check`：通过。
- 受影响回归：359 passed，381 subtests passed。
- 唯一一次最终全量：`python -m pytest -q`，7271 passed、14 skipped、5 failed、1497 subtests passed，482.57 秒。
- 最后一轮 G 和倒数第二轮 F 都没有新问题：G 覆盖 fixture/fallback/冷启动并通过 39 项回归；F 从调用方和状态流复核 CAND-001～005，未发现修复破坏入口或新增死接线。

剩余风险是 Node.js 缺失导致的浏览器 JavaScript 行为未在本机执行，以及真实 provider/CDP、模型质量和完整浏览器 E2E 的环境依赖；这些限制不被用作审查收敛证明。

审查实现提交：`58eec83f`（`Converge iterative audit and harden typed boundaries`）。报告元数据提交：`3c4b0162`。两次提交均已成功推送到 `origin/master`（远端 HEAD：`3c4b0162`）。
