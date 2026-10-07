# 失败恢复与项目搜索：实现及验收记录

这次落实了 Settings / 发送失败的恢复动作，以及空项目搜索。交互约定同步写入根目录 DESIGN.md 的 §5.3、§5.5 和 §5.11。

## 用户能看到的变化

| 场景 | 修改前 | 修改后 |
| --- | --- | --- |
| Settings 首次加载失败 | 上方仍是 `Loading connection…`，下方要求关闭重开 | 原位置显示 `Could not load connection` 和灰色 `Retry`；同一窗口内重试 |
| 别的聊天占用运行 | `Could not send the message` | 确认运行所属聊天后显示 `Another chat is running · Open` |
| API 模型选择失效 | 通用发送错误 | `Select the model again · Choose model`，打开原有模型菜单 |
| 浏览器工作线程暂时忙碌 | 通用发送错误 | `Codey is temporarily busy · Retry` |
| 原因无法确定 | 通用发送错误 | 保持简短的通用提示及原有 Retry，不根据任意错误字符串猜原因 |
| 搜索空项目名称 | 项目被过滤掉，出现没有匹配聊天的提示 | 项目仍可找到，展开显示 `No chats` |
| 搜索不存在的名称 | 多种空提示及空分组可能同时出现 | 只显示 `No matches`，Settings 入口仍固定在底部 |

错误文案沿用既有 `--err-text`，恢复动作沿用灰色文字按钮。没有新增卡片、抽屉、配色或强调色按钮。搜索仍是原位展开、34px 高、透明且无可见边框。

## 行为保护

- 加载失败时表单保持禁用，关闭始终可用；Retry 防重复请求。关闭或重新打开对话框后，旧请求不能覆盖新窗口。
- Retry 成功后，如果用户没有把焦点移到别处，焦点进入 Address；再失败时焦点回到 Retry。
- 保存失败保留填写内容，仅使用既有错误行；不显示会重新加载并覆盖表单的加载 Retry。
- 跳转和模型选择属于原始失败聊天，不自动发送任务。另一个聊天的草稿保持独立。
- 发送 Retry 使用原始失败请求文本，后续编辑的新草稿不被覆盖。
- 项目自身名称独立匹配；聊天标题匹配仍能显示所属项目。搜索临时展开的箭头与实际显示一致，退出搜索恢复保存的展开状态。
- API 选择校验失败的后端响应提供稳定的 `model_selection_invalid` 原因标识；失败请求不会交给执行器。Local 与 Zen 共用此契约，连接失效不会偷偷改用默认模型。

## TDD 记录

行为测试在 [tests/test_ui_recovery_search.py](../tests/test_ui_recovery_search.py)，通过 Playwright 操作实际发布的 HTML/CSS/JS，而非另建示例界面。

1. 首先编写核心测试：**12 failed / 3 passed**，确认旧实现缺少加载 Retry、已知拒绝的恢复动作和空项目匹配。
2. 实现后，首轮核心测试 **15 passed**。
3. 补键盘焦点及搜索箭头断言：**4 failed**，修复后通过。
4. 桌面实操发现当前聊天内 Choose model 动作被发送时的禁用状态锁住；新增直接点击测试先得到 **1 failed**。修复后测试还确认了点击冒泡会马上关闭新打开的菜单，随后修复事件传播。
5. 截图检查发现无结果时 Settings 上移；先增加位置断言得到 **1 failed**，再让空结果区域保留侧栏的剩余高度。

红灯日志保存在本地：

- [焦点与箭头](../.e2e-artifacts/recovery-search-focus-red.txt)
- [立即选模型](../.e2e-artifacts/recovery-search-model-action-red.txt)
- [侧栏底部位置](../.e2e-artifacts/recovery-search-footer-red.txt)

最终回归命令覆盖新测试、已有工作流、原位渲染、UI 静态约定、模块结构和服务端接口：

```powershell
python -m pytest tests/test_ui_recovery_search.py tests/test_ui_workflow.py tests/test_ui_inplace_render.py tests/test_ui.py tests/test_ui_architecture.py tests/test_server.py -q --tb=short
```

本机没有全局 Node，最终运行在测试进程的 PATH 中加入 Playwright 自带 Node，执行了原本会被跳过的 JavaScript 行为检查。最终结果为 **356 passed，2 subtests passed，无跳过**；其中本次专门新增的行为测试共 16 项。结果见 [最终回归日志](../.e2e-artifacts/recovery-search-final-regression.txt)。

另外，全仓 Ruff、376 个源码文件的 mypy、16 个 JS 资源的 `node --check` 和 `git diff --check` 均通过。页面内联脚本未增加预算；分组标题显示逻辑放在现有 ConversationUI 模块内。

## 真实桌面操作与设计检查

使用 [tools/ui_recovery_search_review.py](../tools/ui_recovery_search_review.py) 打开真正的 pywebview / Edge WebView2 窗口，宽窗口为 1380×900，并实际缩小到 500×540。脚本通过鼠标点击、键盘 Enter / Tab / Escape / 方向键 / Ctrl+, 操作既有控件。使用临时 AppContext 和隔离 WebView2 存储；API 故障按场景模拟，没有改动个人聊天或连接配置，也没有向真实模型发送请求。

完整桌面流程覆盖了：连续两次加载失败再恢复、保存失败后保留编辑、空项目与聊天标题搜索、项目名称搜索、无结果、退出搜索、空搜索失焦、原始任务重试、新草稿保护、打开运行所属聊天、重新选模型、未知错误及窄窗口布局。**7 组检查通过，页面 JavaScript 错误为 0**。

可运行同一脚本复查：

```powershell
python tools/ui_recovery_search_review.py
```

结果保存在 [桌面操作记录](../.e2e-artifacts/recovery-search-2026-10-07/native-review.json)。

我逐张检查了前后截图。改善来自两个具体变化：失败提示和解决入口现在在一起；搜索的结果文字能区分“找到了空项目”和“没有找到对象”。Settings 底部入口、原位搜索、原有模型菜单和灰色文字动作均保持既有设计语言。宽窗口和窄窗口中的 Settings 未越出可用视口。

| 截图 | 修改前 | 修改后 |
| --- | --- | --- |
| Settings 加载失败 | [之前](../.e2e-artifacts/recovery-search-2026-10-07/before-settings.png) | [现在](../.e2e-artifacts/recovery-search-2026-10-07/after-settings-failure.png) |
| 搜索空项目 | [之前](../.e2e-artifacts/recovery-search-2026-10-07/before-empty-project-search.png) | [现在](../.e2e-artifacts/recovery-search-2026-10-07/after-empty-project-search.png) |
| 没有匹配结果 | [之前](../.e2e-artifacts/recovery-search-2026-10-07/before-no-matches.png) | [现在](../.e2e-artifacts/recovery-search-2026-10-07/after-no-matches.png) |

其他截图：[Retry 与新草稿](../.e2e-artifacts/recovery-search-2026-10-07/after-worker-retry-draft.png)、[运行聊天入口](../.e2e-artifacts/recovery-search-2026-10-07/after-busy-open.png)、[重新选模型](../.e2e-artifacts/recovery-search-2026-10-07/after-choose-model.png)、[窄窗口 Settings](../.e2e-artifacts/recovery-search-2026-10-07/after-narrow-settings.png)、[窄窗口搜索](../.e2e-artifacts/recovery-search-2026-10-07/after-narrow-search.png)。

以上验证针对 UI 恢复流程和对应接口契约，没有验证真实模型服务宕机后的端到端恢复。`.e2e-artifacts` 是被 Git 忽略的本地验收材料。

## 后续修正：搜索框的蓝色清除按钮

原截图中的蓝色 × 来自浏览器原生搜索控件，不符合 DESIGN.md 的单色图标规范。它在首次交付的视觉检查中被遗漏，此次单独修正。

先新增 5 项测试，确认 **5 failed**，再实施样式及交互修改，得到 **5 passed**。覆盖默认灰色、悬停变亮、SVG 线条与透明背景、鼠标和键盘清空、清空后焦点保留、Escape 退出，以及宽/窄侧栏中的长查询布局。新测试与原有 UI 相关回归最终为 **148 passed，2 subtests passed，无跳过**；首次交付的 356 项回归结果保留在前文。

现在用灰色 SVG 线条按钮替代原生蓝色按钮：默认 `--text-dim`，悬停 `--text`，无边框和底色。输入为空或搜索关闭时隐藏；点击或键盘激活后清空查询，恢复所有结果，焦点回到仍然打开的输入框。保持原有 34px 搜索行高，右侧预留图标空间。

再次打开真实 pywebview / WebView2 桌面窗口，检查默认、悬停、清空及窄窗口状态。完整桌面流程 **8 组检查通过，页面 JavaScript 错误为 0**。在清除图标区域检查截图像素，旧版检测到 38 个蓝色像素，默认/悬停/清空后的截图均为 0；检查范围避开了 Windows ClearType 文字边缘。

- [先红日志](../.e2e-artifacts/search-clear-red.txt)
- [再绿日志](../.e2e-artifacts/search-clear-green.txt)
- [UI 回归日志](../.e2e-artifacts/search-clear-regression.txt)
- [真实桌面日志](../.e2e-artifacts/search-clear-native.txt)
- [前后及不同状态的截图对照，局部放大 3 倍](../.e2e-artifacts/recovery-search-2026-10-07/search-clear-comparison.png)
- 原始截图：[默认](../.e2e-artifacts/recovery-search-2026-10-07/after-search-clear-default.png)、[悬停](../.e2e-artifacts/recovery-search-2026-10-07/after-search-clear-hover.png)、[清空后](../.e2e-artifacts/recovery-search-2026-10-07/after-search-clear-cleared.png)。
