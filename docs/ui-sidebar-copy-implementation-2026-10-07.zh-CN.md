# 侧栏拖动与选中文字复制：实现及验收记录

本次实现可拖动侧栏和选中文字后的右键 Copy，并按反馈去掉 Copy 按钮内的高亮框。交互约定已同步到 DESIGN.md 的 §4、§5.3 和 §5.5。

## 用户能看到的变化

| 操作 | 最终效果 |
| --- | --- |
| 拖动侧栏右侧分隔线 | 调整侧栏宽度，默认 260px，范围 220–420px；记住设置，重新打开及收起展开后保留 |
| 双击分隔线 | 恢复默认 260px |
| 缩小窗口 | 为正文保留空间；窄窗口使用原有覆盖式侧栏，不覆盖保存的宽度偏好 |
| 选中用户消息或模型回复，再右键 | 显示深色小菜单和普通灰色 Copy 文字，只复制选中的部分 |
| 输入框中选中部分文字，再右键 Copy | 复制选中的内容，保留草稿和选区 |
| 鼠标移到 Copy，或使用键盘打开菜单 | 用现有灰色行背景提示当前位置，无按钮内边框或高亮轮廓 |

分隔线平时透明，悬停或键盘聚焦时只出现一条灰线。菜单沿用现有深色面板、细灰外边框和圆角。复制成功短暂显示灰色 `Copied`；失败显示 `Could not copy`，可就地重试。

分隔线支持左右方向键、Home/End；Escape 可取消当前拖动。选区菜单支持 Shift+F10 和菜单键，Escape 返回原选区；点击外部、滚动、调整窗口或切换聊天会关闭菜单。

## TDD 与验证

行为测试位于 [tests/test_ui_desktop_interactions.py](../tests/test_ui_desktop_interactions.py)，操作实际发布的 HTML/CSS/JS。

1. 先写核心行为测试，在旧实现上得到 **14 failed**，再修改至 **14 passed**。
2. 收到 Copy 样式反馈后，先增加无按钮内轮廓的断言，得到 **1 failed**，再修改样式。
3. 最终专项测试 **15 passed**；与现有工作流、搜索恢复、原位渲染、UI 架构及桌面监听生命周期合并回归，得到 **166 passed，2 subtests passed**。
4. Ruff、JavaScript 语法检查和 `git diff --check` 通过。

红灯及绿灯日志保存在本地 `.e2e-artifacts/sidebar-copy-*.txt`。

## 真实桌面实操

[tools/ui_desktop_interactions_review.py](../tools/ui_desktop_interactions_review.py) 使用隔离的数据目录与模拟 API 打开真实 pywebview / Edge WebView2 桌面窗口，无需调用模型或修改个人聊天。

五组操作全部通过，页面 JavaScript 错误为零：实际鼠标拖动并重新加载；用户消息选区复制；模型回复选区复制；输入框部分选区复制及键盘退出；缩小桌面窗口和窄窗口侧栏检查。复制内容通过读取 Windows 剪贴板核对，确认与选区完全一致。

实操截图与检查结果保存在本地：

- [Copy 最终样式局部](../.e2e-artifacts/sidebar-copy-2026-10-07/copy-plain-detail.png)
- [回复选区及菜单完整截图](../.e2e-artifacts/sidebar-copy-2026-10-07/copy-asst.png)
- [输入框选区及菜单](../.e2e-artifacts/sidebar-copy-2026-10-07/copy-composer.png)
- [键盘焦点样式](../.e2e-artifacts/sidebar-copy-2026-10-07/copy-keyboard.png)
- [侧栏拖宽到 380px](../.e2e-artifacts/sidebar-copy-2026-10-07/sidebar-wide.png)
- [桌面验收结果](../.e2e-artifacts/sidebar-copy-2026-10-07/native-review.json)

这些图片来自真实桌面窗口；局部图仅裁剪放大，没有重绘界面。
