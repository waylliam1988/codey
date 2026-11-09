# 浏览器与 Research 确定性收敛实施进度

日期：2026-11-09（Asia/Taipei）。用户授权四批实施并逐批提交；每批补相关测试、运行 KoboldCpp 实机检查、Ruff 与类型检查，再更新文档和 commit。

## 范围与约束

- 仅实施不需要模型收益实验决定的正确性修复和保持行为的收敛。网页默认获取策略、模型工具集和研究提示策略不在本轮范围。
- 冷启动协议保持 v1；不增加旧结构兼容、第二套运行时或恢复账本。
- UI失败说明使用英文，并遵循仓库 `DESIGN.md`；沿用现有工具行和详情展示，不增加冗余状态组件。
- 新增和改动边界使用明确类型，消除所触及路径的动态类型；不添加 `Any`、类型豁免或防御性冗余分支。
- 优先直接相关测试；不主动运行完整 pytest、覆盖率或安装包构建。必要扩大范围时先说明原因。
- 测试失败按契约定位根因，区分测试陈旧与生产缺陷；不以放宽生产验证迁就测试。
- 基线 HEAD：`3d889439`。开始时已有 `codey/runtime.json` 修改和三份未跟踪的分析报告，均不纳入实现提交。

## 四批交付

| 批次 | 内容 | 状态 | 提交 |
|---|---|---|---|
| 1 | 正文版本、文内命中与续读绑定；恢复不重新获取 | 完成 | `7751e6a1` |
| 2 | 来源接收、正文窗口、回执与 passage 共用 | 完成 | 本文所在第二批提交 |
| 3 | 结构化获取/执行/阅读状态、恢复与重放合同；UI失败说明 | 未开始 | 待提交 |
| 4 | 保持行为的工具定义、执行与事实投影去重 | 未开始 | 待提交 |

## 验证与恢复工作说明

实机使用本机 KoboldCpp，通过原生产 headless/kernel/provider 执行；可以控制网页响应以验证版本、读取与错误边界，但模型请求与任务执行保持真实。每批保留原始请求、响应、回执和结果；实机检查用于发现确定性缺陷，不当作回答质量收益 A/B。

类型检查按 `docs/typechecking.md` 执行。新增实验记录器复用现有 local model gate 能力，不另建 agent loop。

### 批次 1

- 已确认现状：`source_search` 定位已保存正文；`open_hit` 的目标仅有 URL/offset/pages，之后重新 fetch；`reopen_source` 也重新获取。
- 命中增加正文 hash，原 source_search 回执携带被检索正文；旧命中、reopen_source 和已打开 URL 的 offset 续读只读原捕获。open_url offset=0 明确重新获取。来源 sN 仍表示 URL，不另建版本账本。
- PDF 扫描页保留在原定位回执，不冒充已读；恢复后读取这些页不联网。扫描仍受 30 页、每 10 页最多 80,000 字符的既有上限约束。修复页内命中偏移丢失，以及 PDF 扫描吞掉 DeadlineExceeded 的确定性缺陷。
- 相关测试 59 passed / 142 deselected；补充页内远端命中后，再运行 29 个版本/定位/原子性用例全部通过。曾扩大到现有 Research 文件以检查共享 PDF 行为，发现 5 个旧测试缺少浏览器事件接口、依赖固定 cancellation.check 次数或旧修复文案，已修正测试并逐项通过，未放宽生产合同。
- Mypy、Pyrefly、ty、kernel precision gate 和改动文件 Ruff 均通过。Pyrefly 现有 warning/suppression 并不等于新增类型豁免；本批未新增 Any 或豁免。
- KoboldCpp/Gemma4 实机：`.e2e-artifacts/browser-research-convergence-20261109/batch1-b/summary.json`，passed=true / done。六个工具步骤、两次 HTTP 获取；实际交付 V2=99 grams、旧 hit=V1 的 17 grams。模型最终解释误述 V2，故不能把此合同验证当成答案质量证明。
- 首次实机 batch1-a 已完成生产执行，但实验汇总器误把已有 helper 返回的字符串当字典；修正实验器后重新实机，未修改生产去迁就实验器。

### 批次 2

- 获取边界生成一次经过校验的来源捕获，并与结果一同交给共享阅读实现；不再在渲染时另造获取时间或重新分类质量。PDF 本次读取回执保留本次页范围，正式投影仍按原合同合并独立页读取。
- 抽出 `tool_output.py` 与 `source_reading.py`，Chat/Research、新获取/捕获重读共用正文窗口、预算、回执和 passage；网页、Research 适配完整读取与已保存 source 也共用 `describe_captured_window`。知识材料原有固定版本、访问范围、证据精确区间与答案发布合同保留。
- source_snapshot 是唯一来源编码校验入口，供原获取、ledger 恢复与知识材料使用。PDF 子集保留原获取时间和元数据，不重新获取。
- 相关 61 个用例通过，包括 HTML/abstract/PDF 三种输入的新读/重读/恢复完全一致、单次获取时间、独立 PDF 页回执、知识历史版本和答案上下文。两个既有测试陈旧：字段列表未包含已存在 acquisition 字段，定位测试把“保留前文”误限定为恰好从段首开始；按真实合同修正后通过。
- Ruff、Mypy、Pyrefly、ty、kernel precision gate 通过；移除适配完整阅读两个函数签名中的动态类型，未新增 Any 或豁免。
- KoboldCpp/Gemma4 真实 Research 入口：`.e2e-artifacts/browser-research-convergence-20261109/batch2-a/summary.json`，passed=true / done。浏览器一次获取，随后 source_search → open_hit 读取原捕获，submit_research_answer 以 [e2] 发布原文 17 grams。实验器最初只把 open_url 计入后续阅读，漏掉合法 open_hit；修正汇总条件后用原始记录重算，无需重跑生产。

### 批次 3

尚未开始。

### 批次 4

尚未开始。
