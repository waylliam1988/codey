# 消息复制图标自动隐藏：实现及验收记录

用户消息和模型回答下方的整条复制图标，在可悬停设备上默认隐藏。鼠标进入对应消息后显示；正文、通向按钮的空隙和按钮属于同一个连续区域，因此移动过去点击时不会消失。按钮的位置始终保留，不因显隐改变消息布局；隐藏时不响应鼠标点击。

键盘 Tab 进入消息控件时显示图标，复制按钮沿用灰色焦点背景。鼠标点过按钮后，焦点本身不会让图标永久显示。无悬停能力的触屏设备保持图标可见。代码块和思考内容的复制入口保留原有显隐规则。

复制进行中保持图标可见，并防止重复操作；使用 `aria-disabled` 和操作守卫保留键盘焦点。成功的灰色对勾显示 1.2 秒；失败显示灰色 `Could not copy`，按钮保持可重试，反馈保留 4 秒。备用复制方式使用临时文本框时，恢复原来的键盘按钮焦点。遵循减少动画的系统设置。

上述约定已写入 DESIGN.md 的 §5.5。本次保留了上一项修改：输入区上方没有贯穿主区域的分隔线，标题下方仍有原有淡灰细线。

## TDD 与回归

测试位于 [tests/test_ui_message_copy_visibility.py](../tests/test_ui_message_copy_visibility.py)，使用实际发布的界面，包含真实鼠标路径和点击、键盘复制、触屏点击、异步防重复、失败重试、长回答折叠及选区保护。

- 旧实现：**9 failed / 1 passed**，已通过的控制测试确认代码块和思考复制入口的既有行为。
- 补备用复制焦点测试：**1 failed / 1 passed**，锁定键盘焦点丢失的问题。
- 修改后专项：**12 passed**。
- 合并已有侧栏/选区复制、失败恢复与搜索、工作流、原位渲染、UI 静态约定、模块架构和桌面监听生命周期：**178 passed，2 subtests passed**。
- Ruff、16 个 JavaScript 资源语法检查及 `git diff --check` 通过。

红灯、专项绿灯及回归日志分别保存在本地 `.e2e-artifacts/message-copy-visibility-red.txt`、`message-copy-fallback-red.txt`、`message-copy-visibility-green.txt` 和 `message-copy-visibility-regression.txt`。

## 真实桌面验收

使用隔离数据与模拟 API，在实际 pywebview / Edge WebView2 窗口中检查了四组操作：用户消息复制、模型回复复制、键盘复制，以及模拟剪贴板不可用时的失败提示。全部通过，页面 JavaScript 错误为零。

鼠标实际经过文字到按钮之间的空隙，确认可以点击；复制结果读取 Windows 剪贴板核对，文本内容完整，Windows 的 CRLF 换行按平台规则进行比较。截图来自真实界面，局部图只做裁剪和放大：

- [默认隐藏](../.e2e-artifacts/message-copy-visibility-2026-10-07/hidden.png)
- [鼠标进入用户消息](../.e2e-artifacts/message-copy-visibility-2026-10-07/hover-user.png)
- [鼠标进入模型回复](../.e2e-artifacts/message-copy-visibility-2026-10-07/hover-asst.png)
- [经过正文到按钮之间的空隙](../.e2e-artifacts/message-copy-visibility-2026-10-07/gap-asst.png)
- [键盘焦点](../.e2e-artifacts/message-copy-visibility-2026-10-07/keyboard.png)
- [复制失败反馈](../.e2e-artifacts/message-copy-visibility-2026-10-07/failure.png)
- [桌面验收结果](../.e2e-artifacts/message-copy-visibility-2026-10-07/native-review.json)
