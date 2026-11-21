# 研究报告坏尾句：确定性拦截与定向修复

日期：2026-11-21，Asia/Taipei。修改基线：`847a9ea4`。

本轮已为原有研究提交增加尾部检查：报告以裸露、未配对的内联代码反引号结束时，不记录成功提交，而是返回 `report_tail_incomplete`，让模型使用同一 submit_research_answer 修复完整报告。既有证据、文件、验证回执继续有效。程序不补写缺失事实，也不删除末句后冒充完整报告。

## 原因与边界

前次原始响应是合法 JSON，服务端 finish_reason=stop；markdown 却结束于“将 code 更新为完整字符串 `”，随后模型声明 answered。客户端没有截断该字符串。原提交验证检查了输入形状、证据、引用和声明字段，但没有拦截这个明确的尾部缺陷。

原始失败报告见 `.e2e-artifacts/unified-repair-natural-repair-fifth/provider.jsonl`。本轮直接读取其实际提交：旧 scope=4 下验证结果和 submission_digest 保持不变；在新 scope=6 下，同一正文会被拒绝，见 `.e2e-artifacts/report-tail-original-replay.json`。

这个检查有意保持狭窄：只处理明确裸露的尾部标记。正常闭合的代码片段、多重反引号、转义字面量、引用块、缩进/围栏代码，以及没有句末标点的正常段落都保留。不以“必须有句号”“不能以冒号结尾”等语言启发式判断报告完整，也不认证推理、结论或没有标记的半句话。

## 实现与历史兼容

- answer_contract 生成新版 scope=6，提交验证在字段槽扩展后检查尾部；报错明确要求修复报告，保留已有陈述和引用，复用已提供的 `{{field:/pointer}}` 槽，避免重复浏览、编辑或检查。
- scope=4/5 的旧回执按原合同恢复、验证，不改写历史正文、引用偏移或摘要。新请求的 scope 来自冻结请求，模型不能通过给工具参数塞入旧 scope 绕过新检查。
- 复用已有 Markdown 代码遮罩。新版尾部检查识别偶数反斜杠后的正常代码起始符；历史引用扫描默认规则保持原样，避免改变已持久化的 citation offsets。
- 失败和修复仍走原工具、原 kernel 轮次/进展限制和原 effect 回执。没有第二个审稿模型、自动续写器、后台重试循环或新运行时。模型重复错误直到原限制耗尽时，任务仍失败。

## 原坏报告 + 本地模型的修复验证

`.e2e-artifacts/report-tail-live-repair-first/` 使用前次保存的前 11 个模型提议重放实际操作：真实 Chromium 取证，真实文件编辑，真实 unittest 首次失败及修复后成功。没有注入成功回执。第 11 轮提交的就是原坏报告；从第 12 轮起才调用真实本地模型。

| 阶段 | 实际结果 |
| --- | --- |
| 第 11 轮 | 原坏报告被拒绝，没有 research_submission 成功回执 |
| 第 12 轮，实机生成 | 模型补齐 QUARTZ_851、闭合代码标记，补回检查结果，重新提交完整报告 |
| 第 13 轮，实机生成 | done 被原完成门接受 |
| 拒绝之后的操作 | 只有一次 submit_research_answer；没有重新浏览、读取文件、编辑或执行命令 |
| 最终文件与检查 | amount=173、code=QUARTZ_851、keep 保留；复用原真实 unittest exit 0 |

这是**历史提议前缀重放 + 实机修复**验证，不能称为一次完整的新自然任务。前缀来源、hash、每轮重放标签、实际模型请求及全部原回执都保留。没有修改用户任务、网页数据、模型参数、权限、文件版本或完成门。

独立的新自然任务 `.e2e-artifacts/report-tail-natural-repair-first/` 仍失败：模型多次把 production-browser-record 抄成 production_browser_record，实际收到 404；之后到达 max_turns，未编辑成功，也没有提交报告，因此没有进入新尾部检查。此失败保留，不用修复验证的通过覆盖它；本轮不能证明完整自然任务始终可靠。

## 回归中的问题及修复

首批扩大回归为 **31 failed / 134 passed**，全部保留在 report-tail-regression-first.log：

- 19 项字段/查询场景的旧实验工具仍向同步 BrowserSearchProvider 注入页面，而生产任务已由 task browser 持有页面；实际请求没有被 fixture 接管，返回 404。实验改为复用已有 ResumableBrowserSearchProvider 和 RouteDecision，页面仍由生产 worker 执行，没有为测试修改生产导航行为。
- 12 项旧断言仍要求 strict Research 隐藏已授权写入工具或删除 done，或期待已经退出的报错文案。迁到统一授权合同，保留非法输入不写入、不注册问题及跨任务 scope 拒绝的覆盖。该测试文件补齐参数和返回类型，使用明确的 ResearchTools/KnowledgeStore fixture。

同一扩大批次修复后 **165 passed，327.43 秒**，包括：字段槽、报告编译/恢复、引用遮罩、完成合同、文本与 native 两条真实浏览器提交链路的坏报告拒绝与修复。后补的旧 scope=5 非空字段兼容与旧 scope 注入拒绝专项也通过，最终纯单元批次 **20 passed / 2 deselected**。未运行全量 pytest。

静态首败包含 Search.fetch 协议返回类型、旧 evidence fixture 形状、HeadlessConnector 参数名，以及新测试 TypedDict 到 JSON 边界的显式转换；均在所属测试/工具边界修复，没有 cast、Any、type-ignore 或检查豁免。最终全树 Ruff、全生产源码 mypy/pyrefly/ty、本轮 6 个 Python 文件的三种类型检查和整文件 AST precision 均通过。

所有失败、重跑、实机原始请求/回执和源码 SHA256 见[验证清单](data/codey-research-report-tail-validation-20261121.json)。上轮记载的全部历史 tests/tools 类型债仍未清零；本轮生产源码和修改文件的绿色不代表该扩大范围全部通过。
