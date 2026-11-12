# 首次请求前访问单个必要来源

基线为 `78b1903f`。A 由模型决定何时调用 `open_url`。B 在首次实际模型请求前，根据冻结任务和回执状态安排一次来源访问。两边使用同一任务文字、模型、生产 CodeMode、`open_url`、完成门和受控 Chromium 页面。

B 只在普通 Chat 中处理一个明确声明且尚未尝试的必要 URL，要求已有 `web.read` 授权，并且当前冻结目录提供 CodeMode 和 `open_url`。实验通过已有的 provider shim 提交一次标记为 `runtime_action` 的 CodeMode 提案，然后由正式工具链执行、结算并交付原始回执。该提案单独记账，占一个内核轮次，不计为模型请求。它不改变生产调度，也不实现跨进程恢复。

403 场景只有在实际尝试后产生失败回执并正式进入 `blocked` 才通过。200 场景要求一次真实成功读取、实际请求含原回执编号和正文记录、最后报告该记录并进入 `done`。B 额外要求首个实际模型请求已含回执和正文或错误，两侧的真实来源调用次数都必须为一。

脚本化回归复现 A 在 403 任务中直接提议 `done`、零次访问的失败，也覆盖两侧真实成功读取和 B 首次请求中的失败回执。另测没有必要 URL、多个 URL、URL 被拒、缺授权或工具、已有成功来源以及已有尝试时不调度。

## 自主实机结果

| 配对 | A 模型发起 | B 实验运行时发起 |
| --- | --- | --- |
| 01，403，AB | `no_progress`，5 次模型请求，0 次访问 | `blocked`，1 次模型请求，1 次访问 |
| 02，403，BA | `no_progress`，3 次模型请求，0 次访问 | `blocked`，1 次模型请求，1 次访问 |
| 03，200，AB | `no_progress`，3 次模型请求，0 次访问 | `done`，1 次模型请求，1 次访问 |

**验收 A 0/3、B 3/3。实际模型请求 A 11 次、B 3 次。** 三个 A 都直接提交了 `done`，有的给出占位符或猜测值，完成门因没有来源回执而拒绝。三个 B 都只真实访问了已声明的 URL 一次。各自第一个实际模型请求里都包含同一次访问的原回执编号和真实结果。两条 403 都根据实际错误报告限制，200 则报告了正文里的 `REQUIRED_SOURCE_RECORD_418`。三个 B 调度动作在日志里单列。

六个单元任务文字逐字一致。实际请求的 seed 均为 123456789，temperature 均为 0，统一使用流式接线。生产源码和实验脚本摘要前后一致，无服务失败或预算中止。实机只使用同一个受控 URL 和一个模型，结果支持该范围内的调度方式，仍需验证其他页面、模型和恢复场景。

27 项相关回归通过，Ruff、strict mypy、Pyrefly 和 ty 通过。未改生产代码，未创建 worktree。

[实机总表](../.e2e-artifacts/required-source-prefetch-20261112/summary.json) · [403 首次请求与回执交付](../.e2e-artifacts/required-source-prefetch-20261112/01-403-B/provider.jsonl) · [200 首次请求与正文交付](../.e2e-artifacts/required-source-prefetch-20261112/03-200-B/provider.jsonl)

## 复现

```powershell
python -m pytest -q tests/test_required_source_prefetch_ab.py tests/test_receipt_bound_edit_surface_probe.py tests/test_browser_production_execution.py
python -u -m tools.required_source_prefetch_ab --run-dir .e2e-artifacts/required-source-prefetch-20261112
```
