# 页面重置两轮完成检查

上一轮实机的四个 `page_reset` 单元都报对了净重和校验码，只执行了一轮导航、填写与点击，仍被标记为 `done`。原有任务声明要求六种工具分别成功，第二轮顺序只在实验结束后评分。本次只验证把两轮要求接入正式完成门的效果。 [上一轮报告](protocol-surface-b4-delivery-live-20261111.zh-CN.md)

## 对照与验收

A、B 使用同一任务文字、受控真实 Chromium、流式模型接线和原有 CodeMode 工具。B 在实验 worker 内为 Chat 注册 `page_reset_two_rounds` 检查，由正式完成门决定 `done` 是否成立，成功时检查进入正式完成证明。两侧均使用基础工具路径，以隔离完成检查的作用。

检查按持久叶回执顺序，从每次成功导航到指定页面开始，匹配真实快照、Locality 填写、Load record 点击、成功等待及带真实校验码的完整正文。新的导航或标签切换会结束未完成的轮次。同页重复读取和父调用回放计数无法替代第二次导航。提前提议 `done` 时，完成反馈指出已完成轮数及首个缺失工具，每次检查另外记录证据回执编号。

脚本化 HTTP 与真实 Chromium 测试复现了 A 在只完成一轮时接受 `done`。B 拒绝同一提案，下一次实际请求含第二轮缺导航的反馈。真实补做后，正式完成证明包含新检查。测试还删除第二轮关键动作的回执，或在点击和等待之间插入新导航，确认无法拼出第二轮。上一批四份原始失败回执用新检查只找到一轮，下一步均为 `browser_navigate`。定向 pytest 4 项通过，Ruff 和实验运行器 strict mypy 通过。

## 实机

```powershell
python -u -m tools.protocol_surface_ab --config page-reset-gate --scenario page_reset --run-dir .e2e-artifacts/protocol-page-reset-gate-live-20261111-01 --repeats 2 --timeout 90 --budget 300 --max-turns 10 --seed 123456789
```

两对串行运行，按 AB、BA 交替。模型沿用 KoboldCpp Gemma4 12B，temperature 为 0。验收要求正式 `done`、两轮真实回执及答案包含实测净重和校验码。单元失败保留原样。

| 配对 | A | B |
| --- | --- | --- |
| 01 | 通过，2 轮，3 次模型调用 | `no_progress`，1 轮，6 次模型调用 |
| 02 | `done` 后验收失败，1 轮，4 次模型调用 | 通过，2 轮，4 次模型调用 |

**A、B 各通过 1/2。** A 有 1 次只做一轮的 `done`，B 为 0。模型调用合计 A 7、B 10。四个单元都正常取得结果，无服务失败或预算中止。退出码 1 来自两个场景验收失败。

- **01/B** 首次提议 `done` 时，第一轮还缺等待与读取。反馈进入实际模型请求后，模型补做了这两步。接下来三次都只提议 `done`，没有再次导航。后续请求确实含“第 2 轮下一步是 browser_navigate”，同时生产检查还指出 `knowledge_reference_invalid`，因为模型提交了无效的 `done.knowledge_refs`。两个阻断并存，不能单独归因于新门禁。
- **02/A** 只完成一轮，答案包含真实净重 173 克和校验码 `QUARTZ_851`，正式完成门接受了 `done`，双轮验收失败。
- **02/B** 自主完成两轮后才首次提议 `done`，正式完成证明包含 `page_reset_two_rounds`。该单元没有收到缺项反馈。

脚本化对照证明了门禁能独立拒绝提前完成，并在补做后放行。本批实机观察到第一轮缺项补做，未观察到模型因反馈而补做整轮第二次加载。这个样本支持完成状态更可信的机制结论，尚未显示两轮完成率提升。

批次源码和脚本摘要前后一致，两对任务文字逐字相同。已逐条核对 17 次实际 HTTP 请求的 seed、temperature 和流式设置，并核对 B 组通过单元的持久完成证明。原始证据见 [summary.json](../.e2e-artifacts/protocol-page-reset-gate-live-20261111-01/summary.json)、各单元的 `provider.jsonl`、`state` 和 B 组的 `page-reset-checks.jsonl`。生产源码和生产提示模板未改。
