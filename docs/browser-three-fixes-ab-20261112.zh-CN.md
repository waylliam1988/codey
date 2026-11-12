# 三项浏览器生产故障的定向 A/B

基线为生产提交 `3c9b05c8`。沿用原 14 项生产验收中的 `legacy_structured`、`reread` 和 `stale_edit`。A 使用生产实现，B 每个场景仅启用对应实验处理。两侧使用同一任务、原验收、真实 Chromium 和同一个本地 Gemma4 12B。按 AB、BA、AB 顺序逐单元运行。模型自主选择动作，任务文字未加入故障触发指令。脚本化故障注入单独计数。

| 场景 | 实验处理 | 验收 |
| --- | --- | --- |
| `legacy_structured` | 在浏览器执行边界，只将独立的 `return window.<属性名>;` 转为属性表达式。保留调用原文，并记录实际求值文本。 | 实际读到 token 和当前 URL，最终答案含正确的业务值。 |
| `reread` | 用同一文档、点击前后的两份完整读取，从成功父调用下的加载后回执提取净重与校验码。发布固定事实句，原模型说明留在审计记录。 | 两次真实读取，最终精确报告净重和校验码。更换文档后旧正文无法用于组装。 |
| `stale_edit` | 复用原始读取对象实验助手。B 把本次程序返回的原对象交给助手，由助手传路径和凭证。写入继续走生产版本检查。 | 一次真实版本冲突、冲突后完整重读、写入正确值并保留外部字段。 |

`legacy_structured` 验证的是兼容现有调用的窄纠错。属性专用入口的采用率由之前的实验单独记录。`reread` 的字段选择器针对 fixture 格式，不是通用网页抽取器。B 的最终文本使用固定事实句，任意自由说明的保留与冲突处理仍需另行设计。`stale_edit` 的助手只传递原读取凭证，正文仍由模型构造。注入用例显式基于读取内容合成正文，自主测试仍可能因模型遗漏字段而失败。

## 故障注入

- `legacy_structured` 两侧收到同一段包含顶层 `return` 的模型回复。A 的真实浏览器求值失败，B 改写后读到真实 token 并通过。普通表达式、显式函数、函数调用、索引表达式、换行导致的空返回和多语句源码均有保持原样的检查。
- `reread` 两侧读到同一真实业务记录，脚本模型在最终答案都写成 `QUARTZ_85`。A 进入 `done` 后事实验收失败，B 从完整回执输出 `QUARTZ_851` 并通过。另有一次加载后重新导航到同 URL 的负例，B 不发布旧文档的值。
- `stale_edit` 两侧用同样的内容变更。A 故意漏传凭证，生产拒绝写入。B 把原始读取对象传给助手，先遇到一次真实版本冲突，再重读并基于新正文修改，最终保留外部字段。B 的脚本明确从 `JSON.parse(unwrap(read).text)` 得到文件内容。

## 自主实机

| 场景 | 生产 A | 实验 B | 观察 |
| --- | --- | --- | --- |
| `legacy_structured` | 通过，4 次请求 | 通过，4 次请求 | 两侧均以合法表达式读取 token，B 的纠错未触发。 |
| `reread` | 通过，4 次请求 | 通过，4 次请求 | B 从点击后的原始回执组装了事实句，模型原提案也已写对校验码。 |
| `stale_edit` | `max_turns`，10 次请求 | `max_turns`，10 次请求 | A 漏凭证及取到 `undefined`。B 有一次真实写入，但写错了文件结构。 |

**A、B 各通过 2/3，各 18 次模型请求。** 三对任务文件逐字相同，生产源码与实验入口摘要前后一致。36 次实际请求均使用预定模型、seed 123456789、temperature 0 和非流式接线。没有服务失败或预算中止。

`stale_edit/B` 前两次把解包后的对象传给 `edit.read`，助手按约束拒绝。第三次传了正确的原读取对象，程序代填了路径和凭证，生产接受写入。但模型直接展开 `unwrap(fileResult)`，把 `path`、`text` 和行号等读取元数据序列化到了文件。目标值写进去了，`external_revision` 只存在于嵌套的原文字符串里，丢失了原有字段位置。两侧真实版本冲突次数均为零。此例说明助手消除了最后一次写入的凭证装配错误，同时保留了全文生成的风险。若继续验证，下一项应单独测试绑定原版本的局部更新，让程序解析和修改文件内容。

17 项定向检查通过，包含现有编辑观察边界回归。Ruff、strict mypy 通过。pytest 退出时有 RAM disk 清理告警。未改生产代码，未创建 worktree。

[实机矩阵与原始汇总](../.e2e-artifacts/browser-three-fixes-20261112/summary.json) · [编辑模型程序](../.e2e-artifacts/browser-three-fixes-20261112/3-stale_edit-B/programs.jsonl) · [最终落盘文件](../.e2e-artifacts/browser-three-fixes-20261112/3-stale_edit-B/project-result.json)

## 复现

```powershell
python -m pytest -q tests/test_browser_three_fixes_ab.py tests/test_protocol_surface.py -k "browser_three_fixes_ab or edit_boundary_rejects or codemode_can_compute or real_conflict_observation_boundary"
python -u -m tools.browser_three_fixes_ab --run-dir .e2e-artifacts/browser-three-fixes-20261112
```
