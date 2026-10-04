# 审查链路审计与收口（2026-10-04）

范围：`39a0fc80`、`de413af3` 两个提交及原有未提交修改。保留并验证了已有的
项目身份匹配、敏感 rename 输入过滤和复用项目绑定。版本保持 0.5.11；本轮不发布。

## 修复了什么

| 确定性问题 | 正式所有者与修复 | 回归测试文件 |
| --- | --- | --- |
| 非法对象后跟合法 approved，或嵌套对象、status 元数据，可能掩盖不完整回复 | `core._json_candidates` 消费整个对象；`parse_review_response` 保留失败/预算诊断，不将 status 当 verdict | `test_review_parser_preserves_incomplete_results.py` |
| 部分结果已有有效问题，格式修复却可能把问题变成 approved | `parse_review_with_repair` 保留有效问题；无可执行问题的 changes_requested 不宣称完整 | `test_review_repair_keeps_findings_and_redaction_scope.py`、`test_review_contract_owner_and_empty_rejection.py` |
| NUL rename 文件名仍被箭头或 tab 猜测拆分 | status/numstat 按 Git 字段顺序解析；显式 previous_path 不再二次拆目标；不完整记录报告失败 | `test_git_nul_rename_preserves_literal_delimiters.py` |
| 普通认证代码被过度排除，私钥正文及部分凭据没有正确遮蔽 | `input` 保留正常模块/长代码字面量，逐行遮蔽私钥和凭据；必要文本截断、遮蔽进入 scope | `test_review_snapshot_and_redaction_boundaries.py`、`test_review_repair_keeps_findings_and_redaction_scope.py` |
| 扫描读取失败被当成干净 inventory；工作区字节相同但 HEAD/index 已变化 | `BoundedScanBudget.read_failed` 有真实消费者；`identity._inventory_digest` 纳入 Git 基准；capture/verify 共用 `_snapshot_row` | `test_review_snapshot_rejects_scan_failure_and_git_change.py`、`test_review_snapshot_and_redaction_boundaries.py` |
| 未知网页模型名被当成可复用模型身份，self-review 保存身份与实际策略不符 | `review_model_identity` 只识别已知本地 provider 配置；服务保存实际 policy/self-review | `test_review_actual_model_and_policy_identity.py` |
| 多次读取产物导致 hash、结果和 identity 不来自同一份字节；宽松类型可能洗掉非法状态 | `load_review_artifact` 一次有界读取、同字节验 hash、严格类型/ID/范围/finding 校验 | `test_review_artifact_strict_read_and_terminal_conflicts.py` |
| 冲突最终事件可由最后一个 approved 覆盖；失败来源或复用链无法可靠恢复 | ledger 投影永久拒绝冲突；`load_recorded_review` 共用正式来源校验，复用链指向原始 fresh 产物，失败 run 不命中 | `test_review_artifact_strict_read_and_terminal_conflicts.py`、`test_review_history_http_and_reuse_lineage.py`、`test_review_reuse_rejects_failed_runs_and_invalid_ids.py` |
| HTTP/提交将非字符串来源 ID 转成字符串，错误输入进入任务 | 参数在正式提交/预留前校验；HTTP 冷读通过 `/api/run_review` 的真实路由和产物校验 | `test_review_submission_rejects_coerced_source_ids.py`、`test_review_history_http_and_reuse_lineage.py` |
| 没有快照的结果可驱动 Writer；已发请求后的异常可能切换 Reviewer 重发 | Coordinator 拒绝无身份或异项目快照；候选切换只用于连接失败，取消/截止直接传播 | `test_review_consumer_and_provider_failure_boundaries.py` |
| headless 只读审查未用固定目标；不可用审查/收集失败报告 done | 使用 `state.get_provider` 进入真实审查服务；终态与失败一致，共用 metadata/terminal 构造 | `test_headless_review_uses_selected_provider.py` |
| 实机门的零请求/不可用结果可能假通过，Windows 只读 Git 对象使存档清理失败 | gate 独立检查请求、完整结果、产物和文件；清理只处理已确认 owned 临时目录中的删除权限错误 | `test_local_review_gate_rejects_unavailable.py`、`test_local_review_gate_cleans_readonly_git_objects.py` |

## 结构与测试卫生

- `run_review_attempt` 只准备一次输入。复用命中在 `new_chat/send` 前返回，不新增请求。
- 冷 HTTP 读取和显式复用共用 `load_recorded_review`；删除三次读取与重复来源 helper。
- `review_result_payload` 是有界机器元数据所有者；终态沿用 `task_done_event`。
- 删除无消费者 finding helper、repair prompt 转发、project review 私有转发层及没有
  producer 的 ledger 事件分支。本轮跟踪的 `codey/` 生产代码净减少 45 行。
- 保留真实平台清理、连接候选和“显式来源未命中则新审查”行为；这些有消费者和测试，
  不能仅因名字像 fallback 就删除。没有新增旧产物迁移或平行运行时。
- 替换一处 `or True`；Coordinator 测试建立真实临时快照。复用 no-send、入口参数拒绝、
  schema 输出等断言检查实际消费者。不能验证浏览器 computed style 的测试改成诚实命名，
  不把文字 tone 测试写成布局证明。
- 新增 15 个回归文件，collect-only 得到 80 条用例。缺陷测试先复现再修；合法控制用例和
  所有者迁移不冒称全部曾经红过。没有新增 skip/xfail、调宽审批或修改历史基线来通过。

## 验证与唯一全量失败

完整数字见 [TEST_REPORT](../TEST_REPORT.md)。静态检查通过；扩大回归 1020 通过；
机器契约 268 通过。单次全量为 **7187 通过、29 跳过、1 失败、1497 子测试通过，449.65 秒**。

唯一失败是 `test_ghost_post_turn_router.py` 中的旧契约：Reviewer 失败仍应 `done`。
这与新的不可用终态冲突。更新后同时断言：`review_unavailable`、review 元数据为
unavailable、Writer 未调用、没有 committed-success Ghost 观察。定向四文件回归
**44 通过，5.06 秒**。第一次新断言把空 tuple 写成 list，已纠正；这不是生产缺陷。
全量之后没有修改生产代码，没有再次全量，也不把这次全量记作零失败。

KoboldCpp 最终 review 冒烟 **1/1 通过**：1 请求、无重试、5.498 秒、756 reported tokens。
最初实机曾因只读 Git 对象清理失败而失败；先锁定该文件系统缺陷再修，保留失败记录。
运行产物存于忽略目录，不提交源码、prompt 或 provider 原始回复。

## 可以确认的收益与边界

可以确认：有效问题更少被丢弃，不完整审查更少误报通过；错误路径、无效快照、冲突
持久结果不会驱动当前修复或复用；正式 headless 和 HTTP 消费者已接通；未知发送结果
不会靠切换 Reviewer 重发。没有增加普通 project 审查轮次，最多一次 Writer repair。

不能确认：所有模型找问题更准确、用户总体成功率更高，或整个项目不存在 bug。实机只测
一个本地模型的一个 review case，未跑完整 native/JSON 矩阵或真实浏览器布局 E2E。

模型身份是 endpoint/model/settings 的配置摘要，不是模型权重签名；服务端换权重而名称
不变无法由该摘要识别。快照有文件/目录/字节预算，超限或读取失败拒绝信任；不对任意
外部并发写入提供原子文件系统事务保证。inventory 和 Git 检查带来额外 I/O，本轮没有
测量大型项目的延迟，不能把行数减少说成性能提升。

测试可锁定这些有界不变量，不能替代完整程序的形式化验证。当前结论是审查链路在收口，
本轮发现的确定性问题已修并有回归；不使用“百分百无 bug”作为验收结论。
