# 按页面和操作顺序验证完成前置条件

A 使用生产完成门 `a56aecd2`。B 在实验进程内通过正式检查注册入口增加一项检查，其他生产检查仍运行。两侧使用同一任务文字、真实 Chromium、同一份脚本前缀和本地 KoboldCpp Gemma4 12B，temperature 0、seed 123456789。前缀执行真实浏览器动作并提交一次提前答案。生产拒绝后才让真实模型接手。调用次数只计实际发送的模型请求；A 提前通过时为 0。

Research 只要求指定页面这一份证据。候选在原始持久回执中匹配指定条件 `text=鱼丸`，找同一 Chromium 主文档随后取得的完整正文，再把提交回执里的 `[eN]` 解析为真实 passage ref，核对它来自这次读取。反馈依次指出缺 `browser_wait`、缺等待后的 `browser_read`、或需要用实际新引用重提。页面 URL 相同的不同标签由主文档身份区分。

`page_reset` 复用已有两轮匹配器核对真实导航、快照、填写、点击、等待和读取，再要求等待条件正确，且等待、正文与本轮导航的主文档一致。候选只反馈第一个缺项。两组都由模型决定后续操作，实验未添加自动导航。

## 首批实机

| 注入场景 | A 生产 | B 检查 | 实际模型请求 A → B |
| --- | --- | --- | ---: |
| Research 已等待，引用等待前正文 | 提前 `done` | 拒绝，模型补读并改用新引用后通过 | 0 → 3 |
| Research 已读新正文，仍引用旧段落 | 提前 `done` | 拒绝，模型用新引用重提后通过 | 0 → 1 |
| `page_reset` 只完成第一轮便提交 | 提前 `done` | 拒绝，模型补完第二轮后通过 | 0 → 2 |

首批 A **0/3**、B **3/3**；误报完成 **3 → 0**。三对使用同样的两条脚本提案。逐份核对了原始回执、B 的首条实际请求和正式完成证明。`old_read/B` 补读后仍先引用了旧段落，收到第二条反馈才切换引用。`old_citation/B` 只改引用，没有再读浏览器。`page_reset/B` 收到第二轮缺导航的反馈后真实执行整轮。

## 补充实机

| 注入场景 | A 生产 | B 检查 | 实际模型请求 A → B |
| --- | --- | --- | ---: |
| Research 缺等待 | 首次拒绝；补等后仍引用旧段落，接受 `done` | 依次指出等待、等待后的读取和新引用，最后通过 | 2 → 5 |
| Research 在同 URL 的另一标签等待 | 提前 `done` | 拒绝；模型反复提交，未在当前页补等，最终 `no_progress` | 0 → 2 |

合计五对共 10 个实机单元。A **0/5**、B **4/5**，误报完成 **5 → 0**，实际模型请求 **2 → 13**。A 较少的请求来自提早结束。这个实验测的是相同提前提案下的门禁与修复，样本不足以推断自主任务的完成率。跨标签用例表明，准确指出缺项可以防止误报，模型仍可能没有执行所需操作。

## 脚本化回归

Research 覆盖没有等待、条件不符、等待前正文、同 URL 的另一标签完成等待，以及有新正文却引用旧段落。两侧均经过同一生产 Headless 入口和真实 Chromium。`page_reset` 的 B 路径分步验证第二轮缺导航、缺正确等待、缺等待后的读取，最后用 12 个不同的成功动作回执通过。

相关 pytest **33 项通过**。Ruff、strict mypy、Pyrefly 和 ty 检查了实验和测试代码。

## 复现

```powershell
python -m pytest -q tests/test_browser_prerequisite_ab.py tests/test_browser_research_gate.py tests/test_research_completion_gate.py tests/test_required_source_coverage.py
python -u -m tools.browser_prerequisite_ab --run-dir .e2e-artifacts/browser-prerequisite-ab-20261112 --max-turns 7
python -u -m tools.browser_prerequisite_ab --run-dir .e2e-artifacts/browser-prerequisite-waits-20261112 --case missing_wait --case other_tab --max-turns 7
```

各批次 `summary.json`、`prerequisite-checks.jsonl`、`provider.jsonl` 和 `state` 保存在上述目录。生产源码摘要在实机批次前后一致。
