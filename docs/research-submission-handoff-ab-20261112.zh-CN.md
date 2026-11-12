# Strict Research 证据就绪后提交的实机对照

基线为 `817063da`。原页面和延迟 1.5 秒的页面各跑一对，顺序为 AB、BA。每对使用相同任务、Chromium、Gemma4 12B、seed `123456789`、10 轮上限和 90 秒单请求超时。两组都显式声明生产 `BrowserResearchSequence`，要求在指定页面等到「鱼丸」、完整读取同一文档并引用该次读取。因此本次 A/B 比较的是**相同完成条件下的提交时机**。两组运行期间生产源码摘要一致。

A 保留生产工具集合。B 只在实验进程内根据正式回执调整下一次模型请求。当前文档缺有效等待时保留采集工具，已有等待但缺完整读取时只开放 `browser_read`。有可引用的完整正文且其他必要操作已成功时，只开放 `submit_research_answer`，提示该次读取的真实引用。生产完成门、答案校验和引用解析均照常运行。

## 实机结果

| 页面 | A 停止原因 | B 停止原因 | 模型请求 A → B | 首次证据就绪后的读取 A → B | 提交次数 A → B |
| --- | --- | --- | ---: | ---: | ---: |
| 原页面 | `no_progress` | `done` | 7 → 3 | 4 → 0 | 0 → 1 |
| 延迟页面 | `done` | `done` | 4 → 2 | 1 → 0 | 2 → 1 |

A 通过 **1/2**，B 通过 **2/2**。模型请求合计 **11 → 5**，证据齐全后的读取合计 **5 → 0**。两对首次证据就绪的轮次相同。B 两项都在就绪后的第一次模型请求中提交，引用真实 `[e2]`，正确交付 173 克和 `QUARTZ_851`，并取得生产完成证明。 [实机汇总](../.e2e-artifacts/research-submission-handoff-live-20261112/summary.json) · [原页面 B 的工具切换](../.e2e-artifacts/research-submission-handoff-live-20261112/original-B/handoff.jsonl) · [延迟页 B 的工具切换](../.e2e-artifacts/research-submission-handoff-live-20261112/delayed-B/handoff.jsonl)

## 回归与生产建议

脚本化 HTTP 回复走真实 Chromium 和生产提交校验。42 项相关回归通过，其中 9 项针对本次交接，覆盖证据齐备、先读后等、正文截断、同 URL 重载、提交参数格式错误、引用语法错误、未知引用、引用旧正文，以及提交后页面重载导致证据失效。格式和引用问题继续提供提交入口，证据失效后恢复采集，补齐后重新提交。测试同时检查实际 HTTP 请求的工具契约和证据正文。

建议合入生产的范围是**显式声明 `BrowserResearchSequence` 的单页面 strict Research 任务**。可复用生产的等待、正文覆盖和引用核验，按当前文档的回执决定模型下一步。原 full-surface 的 Research 场景要覆盖这条路径，需在任务入口声明该顺序。两对实机都观察到交接机制生效，样本范围为同一模型和同一页面的两种加载时序。多来源 Research、其他采集策略和原生工具调用仍需独立验证。实验脚本未改动 `codey/`。

```powershell
python -m tools.research_submission_handoff_ab --run-dir .e2e-artifacts/research-submission-handoff-rerun
```
