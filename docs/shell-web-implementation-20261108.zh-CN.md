# Shell / Web 三批实施记录（2026-11-08）

## 授权与范围

用户授权三批实施、每批单独提交；先本地 KoboldCpp Gemma4，再 Cline Solar Mini4。相关测试优先，不运行全量 pytest、覆盖率和安装包构建。
执行 Ruff、全生产树 mypy/Pyrefly/ty 及 kernel precision gate，检查器串行运行。新增代码使用明确类型，消除所重构边界的显式 Any；不声称历史代码库已无 Any。
遵守 `DESIGN.md`，本次不改变 UI 外观，状态来自真实执行记录。

## 三批交付

| 批次 | 提交 | 结果 |
| --- | --- | --- |
| 第一批 | `fc0cd737` | 公共执行回执、审批/恢复事实保留、明确联网意图、指定首页直接获取 |
| 第二批 | `d26b7a9f` | Windows 默认优先 PowerShell、任务内 Shell 绑定、退出码作用域、统一捕获/存储解码 |
| 第三批 | 本文件所属提交 `fix: verify execution claims and close unavailable web tasks` | 输出恢复与分页、执行结论核对、普通聊天 HTTP 搜索、不可用来源诚实收尾、真实美食来源验收 |

三批验收均已完成。第三批最终相关 pytest **206 passed**（19 个模块）；Ruff、全生产树三套类型检查及 precision gate 通过。
Gemma 与 Solar 的生产审批、禁止联网、原站不可用收尾和最终美食来源验收均通过。第三批随本文件提交，详细证据见 batch3 报告。

## 第一批完成项

- 公共 `ExecutionReceipt` 集中展示状态、退出码、作用域、截断及执行输出引用。
- 审批重建保留执行事实，首次投递、原生/兼容协议和历史恢复使用一致契约。
- 意图识别覆盖“上网看看”“打开网址看看”，保留明确否定、代码引用及显式禁用约束。
- 用户指定 URL 使用有界 HTTP 获取；研究候选继续使用质量筛选。重定向和 PDF 保持网络策略。
- 冻结旧回执作为历史基线，修正 PowerShell 验收预期。

相关旧回归 46 项、新增/诊断回归 46 项通过；Ruff、三套类型检查和 precision gate 通过。
Gemma 与 Solar 均通过 CMD/PowerShell 生产 HTTP 审批、明确禁止联网和可读首页获取。
报告：`reports/shell-web-batch1-20261108.json`。

## 第二批完成项

- Windows 发现顺序：用户显式配置 → `pwsh` → `powershell` → CMD；失败不自动切换解释器。
- 原生参数数组报告 `process` 作用域，Shell 报告 `shell`；不统一追加 `exit $LASTEXITCODE`。
- 捕获优先 UTF-8；无效时仅回退到主机 Windows 代码页，并显示编码说明。实时存储与最终捕获共用解码。
- 正确处理 UTF-8/DBCS 裁剪边界和被丢弃字节中的无效编码；迁移 SQLite 输出元数据。
- 修复确定性缺陷：旧观察队列过载丢字节。现在同步有界捕获字节，异步合并刷新通知。
- 存储失败的 `unknown` 状态不再被结算覆盖；快照和观察边界使用明确类型。

相关捕获/取消/执行测试 126 passed、1 skipped；补充存储/删除/状态/作用域回归 59 passed；补充 gate 回归 19 passed。
两模型均正确读取原生 7、PowerShell 1、后续输出 0、显式返回 7 的数值和作用域。
Solar 曾在纯中文字段保留编码说明：事实正确、精确格式不符，分别记录，没有为此重复调用。
生产审批、Ruff、三套类型检查与 precision gate 通过。
报告：`reports/shell-web-batch2-20261108.json`。

## 第三批实施

- 分开报告展示截断、存储截断、采集丢失和完整读回能力。展示省略可读回；已丢弃字节、未知状态和未执行回执不承诺完整执行日志。
- `read_tool_result` 复用已有执行数据库，核对 session/run 所有权；没有 `value` 时提示支持 `output`。
  分页明确指向原始引用和下一页偏移，避免转读分页结果自身的回执。读取失败不授权重新执行。
- `done.execution_claims` 绑定真实回执，核对状态、退出码、作用域；未知退出码省略，不能变成 0/null/字符串。
  要求汇报执行状态的任务必须覆盖已观察回执；纠错提供可复制的真实结论。
- `done.source_gaps` 只接受本任务实际失败的获取回执，并核对明确请求的 URL。
  参数错误、策略拒绝和无效偏移不能冒充获取失败。无页面的普通聊天以 `blocked` 收尾，不生成成功证明；严格 Research 保持原契约。
- 每轮当前事实补齐已打开来源和实际失败，避免来源进度仅存在于工具正文。
- 修复 HTML 来源定位偏移：打开位置现在覆盖定位预览中出现的前文，避免预览含某道菜而打开后的正文从其后开始。
- 普通聊天默认使用有界 HTTP 搜索（Bing RSS）及页面/PDF 获取，完整保留中文查询；不自动启动 Research 浏览器，保留显式注入提供者。
- 审核命令状态卡片和执行输出，现有逻辑读取真实状态，无需改 UI；相关 Vitest 10 项通过。

## 实机发现与处理

| 发现 | 归因与处理 |
| --- | --- |
| Windows 使用 `head`、`;`，模型看不到退出码 | Shell 语法混用与回执丢失是独立问题；绑定 Shell，并在首次结果显示实际执行事实 |
| 原站 `api.lihe.chat` 读取不到正文 | 约 233075 字节的 SPA HTML，大型内嵌图标在前、静态正文为空；真实为 `page_blank`，不能据此宣称已验证网站用途 |
| 模型传不存在的 `question_ref=q1` | 参数错误，保留校验并允许修正；不改成普通聊天必须先做研究计划 |
| 内部 `sys.exit(7)`，PowerShell 观测为 1 | 正常 Shell 语义；验收真实值和作用域，不按命令文本猜测 |
| Solar 先报 7，后报 1，但状态仍写 `completed` | 完成核对正确拒绝；增加可复制的真实 `exit/1/shell` 结论后通过 |
| Solar 空页反复获取 | 每轮工作视图缺失败事实；补齐后实际请求一次即诚实 blocked 收尾 |
| Solar 美食首次耗尽轮次 | 已读正文却重复读取，转读分页结果回执；修正分页目标及当前来源进度，保留首轮失败 |
| Solar 美食第二次有一道菜只有预览支持 | 定位预览含匹配点之前的内容，打开偏移可能跳过该内容；回归复现后修正偏移，不把该次回答重算为通过 |
| 定位修复后一次仍引用未打开段落 | 模型选了另一定位，却使用别处预览中的名菜；保留为未通过，最终验收明确仅从实际打开段落选名称，两模型通过 |
| 可读首页一次 TLS EOF | 环境瞬时失败，真实 blocked 回答正确；独立 HTTP 探测恢复后复验通过，未改写生产逻辑掩盖错误 |
| 两个入口单测缺字段 | 旧 SimpleNamespace 夹具缺必需字段及 ExecutionEvidence；修夹具，不放宽生产校验 |

## 证据与复现

汇总见 `reports/shell-web-batch3-20261108.json` 及对应中文报告。
原始请求、事件和响应保留于本机 `.e2e-artifacts/shell-web-batch3-*`，不提交原始模型载荷。失败尝试与最终通过分别记录；重分析保存证据不重复调用模型。

```powershell
python tools/shell_web_diagnostic_gate.py --models gemma --only e2e web --shells pwsh --expect-unavailable --run-dir .e2e-artifacts/new-local --timeout 180
python tools/shell_web_food_gate.py --model gemma --run-dir .e2e-artifacts/new-local-food
python tools/shell_web_food_gate.py --model solar --local-report .e2e-artifacts/new-local-food/summary.json --run-dir .e2e-artifacts/new-solar-food
```

本地先通过，再单独执行 Solar；食品 gate 要求已通过的本地报告。前两批历史报告不修改。

## 验收边界

- 一轮实机不是模型成功率评测，也不能证明所有维度超过 Codex。
- 结构化执行结论由程序核对，任意自然语言的全部语义仍需评测。
- 食品 gate 核对实际查询意图、打开来源及交付正文中的食品名称，不以搜索摘要替代正文。
- 静态 HTTP 不能保证解析 JavaScript、验证码或登录页面；普通聊天不自动启动浏览器。
- 无法恢复已经在 PowerShell 内部转换丢失的字节；应让程序输出 UTF-8 或使用原生参数数组。
- 未运行全量 pytest、覆盖率、安装包构建或重新发布前端。
