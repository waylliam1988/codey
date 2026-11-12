# CI 失败归因与修复（2026-11-12）

依据用户提供的 CI 日志和当前 master 源码检查。本次不运行 pytest，也不创建 worktree。

## 生产与发布问题

- **运行时发布清单过期**：`codey/runtime.json` 漏掉 7 个已提交的 Python 模块，并引用旧前端资源。`read_runtime` 的完整性拒绝是正确行为；使用 `python -m tools.publish_runtime` 重建清单，没有放宽校验。这解释了发布、外部控制、桌面启动测试中的共同初始化错误。
- **历史快照解码兼容性**：旧版快照中的 ToolSpec 尚无 namespace、effect、evidence_route、preconditions。解码器此前按当前字段集合拒绝它们。现在仅接受版本 1–3 的明确旧字段集合，补齐展示元数据，并强制保持 implementation_compatible=False。部分缺字段、额外字段继续拒绝，历史样本保持原样。
- **工具提示泄漏**：`_result_context` 的短句柄提示遗漏 receipt_recovery 条件，在整任务 Research 禁止该恢复提示时仍推荐 read_tool_result。补上同一条件，保留原有“不得宣传不可用工具”的测试断言。

## 过期或不完整的测试

| 分组 | 原因与改动 |
| --- | --- |
| CLI、entry、native policy 假对象 | 补充 allow_browser、entry_session；权限使用真实 TaskPolicy，运行证据使用 RunWork/ExecutionEvidence；配置冲突测试直接调用对应校验，不再构造无 durable 的无效入口 |
| 生产 headless 脚本夹具 | 注入已有 scripted_local_api_connection，避免脚本测试探测开发者或 CI 的真实 Local 端点 |
| JSON 解码、完成门 monkeypatch | 接受新增 object_pairs_hook、observations 参数；私有完成检查传入 report 参数 |
| 浏览器资源关闭 | 假页面显式登记为夹具拥有的页面，假会话提供实际生命周期字段；不让生产代码关闭借用页面来迎合断言 |
| CDP 启动 | Windows Chromium 写 DevToolsActivePort 时可能暂时占用文件或尚未写完；在原有启动截止时间内重试，不修改生产浏览器逻辑 |
| 工具清单边界 | native 只比较 model_specs；另行保留对 frozen_specs 与完整嵌套 inventory 的一致性断言 |
| Research 夹具 | 使用真实 TaskSession，移除已退役的 question_ref 参数；跨任务 scope 使用合法但属于另一任务的 AnswerScope；假工具补齐 browser_session |
| Qwen 重复调用回放 | 保留原 wire、reasoning、call id、回执及停机断言，用仍授权的 knowledge_search 代替已退役 research_plan |
| CodeMode 结果编码修复 | 显式返回含 undefined 的对象触发编码失败，避免依赖已受约束的不存在的浏览器字段；保留只上传一次的断言 |
| 进程输出 | 明确指定 UTF-8 后备编码以测试有损分支；更新退出码作用域、输出可恢复性等已存在的模型文本和 audit 字段 |
| 项目检查事实 | 使用真实 kernel 产生编辑与工作区证明，只脚本化外部 npm 命令，不再伪造成功 RunResult 代替证明 |
| 提示词快照 | 审阅并更新 Result<T>、当前 done 契约、移除旧 question_ref 后的完整快照与哈希；不更改历史 legacy baseline |
| legacy/current 差异记录 | 两个 reopen/open_hit 场景复用已固定的源快照，不再二次 fetch；只更新显式 intentional delta 中的该操作差异 |
| 架构大小债务 | 按当前已提交功能的实际行数登记 model.py=1014、tool_spec.py=1207，并收紧 runtime.py=1282；1000 行阈值和精确增长检查保持不变。这是既存架构债务登记，不是运行错误 |

## 异步失败的边界

- remembered approval 的参数化应用此前共用进程级会话 worker key；现在每个临时应用使用独立 key，同一场景的两次任务仍共用同一 key，继续验证记忆授权。
- 浏览器授权测试的 HTTP 202 只表示决定已接受；现在先等待工作流结算，再检查输出 DOM，保留执行次数、原任务身份和重试幂等断言。
- 未重跑这些并发场景，不能仅凭日志证明上述夹具问题就是全部偶发失败的原因；后续 CI 若仍失败，应结合已有线程栈与 durable 状态定位，不能只继续扩大超时。

## 验证

- 修改的 Python 文件通过 Ruff 和 AST 解析；Linux 和 Windows 平台 mypy 检查均通过（673 个源文件）。
- 正式运行时清单读取成功，完整登记 813 个交付文件。
- 直接检查历史快照可解码但不可执行，部分/未知旧字段被拒绝。
- 通过生产提示生成器更新提示词材料，并使用隔离的脚本 provider 生成两个 Research 差异材料；没有调用 pytest 或测试函数。
- 本次没有全量回归通过的结论，运行与平台行为仍由后续 CI 验证。
