# 第 4 批：来源完成门与交付

验收通过，独立合并；相关 pytest 与静态检查，不要求全量 pytest 或性能对照。

删除来源准入的 ledger/session 替代分支及证据 URL 的异常回退。session 的来源 ID
仅解析别名，最终读取资格必须通过 committed ledger。proof ref 绑定完整 URL、
正文摘要和采集时间，长 URL 不再因展示裁剪造成身份碰撞。

共同 gate 新增 `research_citations`：严格研究要求引用；普通来源任务校验已有引用，
仅在任务显式要求该检查时强制引用。普通解释、创作及无引用契约的 JSON 不受影响。
引用扫描复用 `utils/citation_scanner.py`，来源行复用 `report_quality.parse_citation_rows`，
来源打开及页读取资格复用原 ledger。不建立第二个来源表、模型评委或完成状态机。
页码 0、倒序及超过 100 页的范围改为明确拒绝，删除偷偷改正/截断的路径。

界面 receiptSummary 仅从既有 completion_proof 的 evidence_refs/limitation_refs 渲染
依据和限制，不把模型写入展示字段的来源宣称变成依据，也不改写 final answer。
前端产物通过原 publish_frontend 唯一发布入口更新。

参考 DeepSeek Harness `packages/web/tool-web/src/search.ts:109` 保留结构化来源、
展示不作为事实的边界；本批的确定性 gate 继续扩展 Codey 自有 CompletionContract、
ResearchLedger 和报告编译器，不声称三个参考项目具有同样的本地引用准入实现。

验证：

- 完成门相关选择：312 passed、64 subtests passed；研究/交付最终选择：244 passed、
  7 subtests passed。新增及恢复/身份/浏览器定向选择最终为 48 passed。选择有重叠。
- Ruff、mypy（513 源码文件）、Pyrefly、ty 通过；目标 Python 3.11。现有警告/抑制项保留。
- 前端 TypeScript 类型检查、格式检查、render.test.ts 的 13 项测试通过；生产构建及
  publish_frontend --check 通过。真实 headless Chromium 加载已发布页面与 assets，
  检查依据/限制展示和独立 JSON 正文，1 passed。
- 实机原生/文本的重定向和失败共 4/4 通过，来源恢复与重复恢复一致。原始材料在
  `artifacts/source-completion-live-20261024-01/`。
- 同批页内定位实机两种协议仍跳过 open_hit，与原版相同。其事实恢复正确，但不能称
  完整流程通过。当前来源契约没有把每个自然语言步骤编译为工具顺序要求，保留已知限制。

相关测试发现的四项差异已经归因：两项旧替身仅手工填读取/搜索缓存，改为声明真实
ledger 观察；另两项断言关心首先提示来源未打开，保持先检查读取再检查引用的次序。
没有放宽引用、页码、调用次数、恢复一致性或 JSON 正文断言。
