# 并发事实身份与恢复修复（2026-10-24）

基线：`c6581b97`。本批修复三个已复现的行为问题、一处异常触发的签名兼容重试，以及五项被锁复制异常阻塞的契约测试。

## 复现与修复

| 问题 | 基线行为 | 修改后行为 |
| --- | --- | --- |
| 并行读取 A/B，B 先提交 | 实时 s1=B、s2=A；重开 SQLite 后恢复为 s1=A、s2=B | 在提交锁内分配编号，将 `source_id` 写入同一个 ToolResult 回执；恢复只使用已提交绑定 |
| 并行读取同一 PDF 的第 1、2 页 | 两次成功提交，最终只剩第 1 页 | 读取回执只携带本次采集；共同的来源合并函数保留两页及各页正文 |
| 同轮两个 native CodeMode 父调用执行相同代码 | 第二个子调用结算失败：`settlement call_id differs from intent` | 子 scope 使用已有 `parent_execution_id`，不同 native call ID 对应不同子 effect ID |
| sink 内部抛出包含 scope 的 TypeError | `begin_turn` 被再次调用 | 保留首次异常，生产路径不再尝试旧签名；测试 sink 显式接受 scope |
| 契约测试复制 session | 五项在 deepcopy RLock 时失败，未进入事实断言 | 测试通过 deepcopy memo 保留原锁身份，仍深复制其他状态并检查无部分写入 |

新增回归位于 `tests/test_concurrent_fact_identity.py`。将同一组关键用例放入独立的基线 checkout，得到 9 项失败：三个行为问题对应 3 个用例，异常重试 1 个用例，锁复制 5 个用例。修改后均通过。

## 投影规则

- 来源编号分配不改变正式状态。只有原有 sink 成功提交后，session 和 ledger 才发布投影。返回给调用方的 ToolResult 与持久化的编号绑定一致。
- 显式编号冲突、同 URL 绑定多个编号均拒绝；恢复后再分配编号使用现有最大编号，避免逆序重放造成覆盖。来源引用映射按编号排序，来源记录按采集时间和 URL 排序，使展示顺序也稳定。
- `ResearchLedger.merge_opened_source` 同时服务采集和回执恢复，替代原有 PDF 采集合并与恢复整条覆盖的两套规则。
- PDF 已读页取并集。重叠页按已观察的采集时间选择正文，同时间以正文摘要打破平局；时间先统一时区。合并正文按页码排列，并重新计算投影正文摘要。原始采集正文仍完整保存在各自回执内。
- 页版本表只是从现有观察回执重建的内存投影，随 ledger 一起克隆和发布；没有新数据库、新日志、双写或第二个执行器。
- 读取使用空的临时采集 ledger，防止旧克隆中携带的历史页面冒充本次采集；其他需要既有上下文的工具继续使用隔离克隆。
- 相同 CodeMode 父调用重放不重复执行子工具；两个不同 native 父调用即使代码相同，也分别保留子回执引用。

## 恢复边界

旧来源回执若未保存 `source_id`，无法证明历史中的 s1 对应哪个并发来源。本批明确拒绝这种来源事实的恢复，不从展示文字、意图顺序或请求参数猜测编号，也不伪造迁移后的回执。该限制有独立失败测试；不影响已完成历史记录的展示。

## 验证

- 核心相关集合：211 项通过。
- 扩大相关集合（18 个测试文件，非全量 pytest）：362 项通过、4 项失败，另有 7 项 subtest 通过。
- 四项失败均在未修改的 `c6581b97` checkout 上单独复现，属于旧提示文案断言：
  - `test_research_iteration_controller_rewrites_result_id_before_dispatch`：仍要求 `Available result IDs`。
  - `test_source_search_finds_chinese_html_phrase`。
  - `test_source_search_pdf_locator_does_not_satisfy_page_evidence_until_page_opened`。
  - `test_source_search_requires_opened_source_and_finds_late_html_offset`。
  - 后三项仍要求采集层输出 `Use open_hit from the next allowed-actions block`。
- Ruff 通过；Mypy 检查 519 个生产文件通过；Pyrefly 0 errors（仍有既有 warning/suppression）；Ty 通过。类型检查目标均为 Python 3.11。
- 实际使用 SQLite durable owner、managed result receipts、重开数据库后的生产恢复入口和 Node CodeMode runtime。仅外部来源采集使用可控夹具，以固定完成顺序；没有新增测试专用执行器或进行新的模型质量评测。
- 覆盖提交前失败、提交后中断、重复恢复、逆序完成、页重叠更新、跨时区观察时间、重复 PDF 观察以及同轮重复父调用。提交故障测试沿用现有 `test_committed_research_projection.py`。

在上述相关范围内未发现新增失败；不据此宣称全量测试通过或所有并发投影问题已清零。本批未修改或清除生产数据，也未切换正在运行的桌面服务版本。
