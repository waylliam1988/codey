# 输入框附件区域收起工作记录（2026-11-09）

## 目标与范围

- 移除可见的 `Attach files` 按钮及其专用文件选择器。
- 无附件、无错误时附件区域不占空间；有附件或错误时展开，清空后收起。
- 保留拖放文件、粘贴图片、缩略图、移除、上传重试、按聊天保存草稿、可靠队列及模型图片能力检查。
- 仅提交本次改动，不纳入工作区已有的聊天搜索实验与工具反馈改动。
- 不运行完整 pytest、覆盖率或安装包构建测试；优先验证附件与输入框相关流程。

## 当前进度

- 已确认 `240e329e` 新增顶部附件行与 `8px` 内边距；拖放事件绑定在 `.composer-box`，粘贴事件绑定在文本框，不依赖按钮。
- 已移除按钮、专用文件选择器及按钮样式；附件区域由实际附件/错误状态控制显隐。
- 已补充空区域收起、错误显示、草稿恢复及桌面/窄屏尺寸回归测试；浏览器测试改为从保留的拖放入口上传文件。
- 定向测试还复现了一个已有生产问题：`drop` 在没有可读文件时提前返回，未清除拖放高亮。已将清除操作移到文件检查前，并增加回归测试。
- 已更新中英文 README、Changelog、设计基线与附件契约。
- 已生成并同步仓库内 `codey/web` 前端资源，前端严格类型检查、格式检查与 Python 改动文件 Ruff 通过。
- 已完成图片刷新后恢复和布局截图补强，复验通过；桌面/窄屏截图已人工检查。
- 实现、定向测试、静态检查、文档及前端资源同步已完成，随本次提交交付。

## 验证计划

1. Vitest：附件组件、输入队列、输入框及类型边界相关测试。
2. Chromium：实际服务与附件路由，1280px/480px 下空输入框尺寸、拖入图片展开、删除恢复、粘贴、重试、刷新恢复、能力校验、队列恢复。
3. TypeScript 严格类型检查、改动文件格式检查、Python 改动文件 Ruff。
4. 前端生产资源构建与 `tools.publish_frontend --check`，确保应用实际使用新资源；不涉及安装包或打包配置。

## 验证结果

- 改前运行 `npm test -- src/attachments.test.ts`：4 通过、5 失败。失败准确覆盖未实现的收起行为和已复现的空 drop 高亮残留；未通过改写生产行为迁就旧测试。
- 改后 `npm test -- src/attachments.test.ts src/input_outbox.test.ts src/type_hygiene.test.ts`：26 通过。
- `python -m pytest tests/test_input_attachments.py -q`：19 通过，覆盖真实存储、可靠入场及图片协议投递。
- 首轮 `python -m pytest tests/test_ui_attachments.py tests/test_ui_composer_primary_action.py -q`：18 通过；桌面/窄屏空附件行零占位、拖图展开与删除恢复、粘贴、重试、会话隔离、刷新、模型能力、Steer 和回执丢失恢复均通过。
- 补强后再次运行 `python -m pytest tests/test_ui_attachments.py -q`：11 通过，无退出清理警告。增加图片刷新后缩略图/能力错误恢复检查，以及空输入框、带图片、删除后的桌面/窄屏截图，位于 `.e2e-artifacts/attachments-compact-ui/`。
- `npm run typecheck`、`npx biome format src/attachments.ts src/attachments.test.ts`、`python -m ruff check tests/test_ui_attachments.py`、`git diff --check` 通过。
- TypeScript AST 检查确认 `attachments.ts` 与 `attachments.test.ts` 的显式 `any` 为零；新 Python 测试及改动的上传辅助函数均使用明确参数/返回类型。未修改 Python 生产代码。
- `npm run build`、`python -m tools.publish_frontend`、`python -m tools.publish_frontend --check` 通过；这是仓库内浏览器资源同步，没有发布版本、安装包或修改打包配置。
- 共验证 63 个不同测试用例（前端 26、Python 存储/投递 19、浏览器 18）；没有运行完整 pytest、覆盖率或安装包测试。

## 测试退出清理警告

首轮后端入场测试及包含旧输入框用例的浏览器测试均通过，但进程退出时报告 RAM 盘临时目录中 `.durable.sqlite3.lock` 仍被占用；最终附件 UI 单独复验未出现警告。
附件入场测试用 `submit_browser_task=True` 替代真实 worker，留下已提交输入和活动运行；
`ConversationRuntime.close` 对这些未结束投递返回 `False`，因此应用上下文不会关闭数据库。
这属于测试未模拟 worker 结束的资源清理问题，不能靠放宽生产关闭条件修复。
本次不修改共享关闭流程或已有运行生命周期行为。
