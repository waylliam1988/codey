# KoboldCpp 重启后的浏览器实机复验

日期：2026-11-14，Asia/Taipei。验收基线 HEAD：`b2d7e9649ed186f0d575c69f8872c0b4e7b5be15`，与上次报告的 `51178336` 不同，因此跨次结果差异不能完全归因于 KoboldCpp 重启。

模型：`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，seed `123456789`，temperature 0，请求超时 180 秒。真实 Chromium、生产入口与受控网页夹具；未改工具合同、未改生产代码。此次仅重跑实机，没有重新运行 pytest、Ruff 或 mypy。

原始记录目录：`.e2e-artifacts/browser-actions-restart-20261114-072309/`。本文件归档该次复验结果；完整请求、回执和独立重跑记录仍保留在原始目录。

## 结果

| 验收 | 本次首次 | 独立重跑 |
| --- | --- | --- |
| 完整 full-surface 12 场景 | 11/12 | 只重跑失败的 structured，仍失败 |
| structured_actions_live_gate / Chat | 合同通过 | 未重跑 |
| structured_actions_live_gate / Hybrid | 失败：no_progress | 失败：max_turns |

full-surface 首次：basic、legacy_structured、reread、stale_ref、stale_edit、upload、download、strict Research、strict Research sequence、source_failure、fixed_failure 均通过。reread 恢复快照 10 次；stale_edit 实际触发一次版本冲突，完整重读后恢复，最终文件正确。两个来源失败场景按正式失败回执阻断，符合各自验收预期。

## 失败证据

- full-surface / structured 首次及独立重跑均已取得净重 173、校验码 QUARTZ_851、token 和 URL，所有要求的浏览器动作成功。模型把净重和校验码写入 report_sections，却没有写入实际交付的 done.summary；说明另有字面量反斜杠 n 和重复 URL/token。运行 stop_reason=done，但 facts_match=false，所以实机 gate 正确判失败。分类：模型输出内容与格式错误。未改写模型输出或放宽合同。
- action Hybrid 首次：浏览器要求全部成功；原始任务和冻结 Schema 仍公布 edit_json，但模型从未调用该动作、read_file、edit 或 done，最后连续重复读取。下一请求明确包含“不要以相同参数重复读取”的提醒，模型仍继续；只读无进展停止正常生效。版本冲突注入未到达。分类：模型规划错误。
- action Hybrid 独立重跑：模型反复 snapshot/click/read，未调用 wait、eval、edit_json 或 done。第 18 轮原始请求仍有完整、已通过冻结校验的 wait 与指定 token eval 调用建议；请求历史包含效果循环提醒，模型仍未推进。最终耗尽 18 轮，版本冲突注入未到达。所有实际工具回执成功，未发现环境类失败。分类：模型规划错误。
- action Chat 合同通过，但两段排版诊断 requested_shape_valid=false。模型首次 done 缺 wait，被拒绝后补 wait/read，再正确交付实测事实。现有 gate 的 passed 不包含两段排版诊断，因此保留该限制。

本轮没有发现新的生产确定性问题。普通 CodeMode 的 Hybrid 冲突恢复通过，不能替代新 action Hybrid 的编辑验收；后者仍未通过。

## 记录与完整性

- `full-surface/summary.json`、`structured-actions/summary.json`：本次首次结果。
- `full-surface-structured-rerun/summary.json`、`structured-hybrid-rerun/summary.json`：独立重跑结果，未覆盖首次失败。
- 各场景 `provider.jsonl`：原始模型请求与响应；`state/durable.sqlite3`：正式执行回执。
- 各失败场景 `failure-audit.json`：读取原始请求、响应和正式回执形成的只读审计副本。
- 四批实机 summary 的 source_before/source_after 均相同，且均为启动时的生产摘要 `6dc09dcf684fb72470a7effae6f2e90cde81d2fae20bee7a5cc4ee5d3ac9cafa`。
- `before.json`、`after.json`：收尾时检测到外部新增生产修改，完整运行时实现指纹与工作区状态已改变，HEAD 与原有两个外部文件哈希保持一致。结果只对应各批实际运行时的源码，不证明收尾时新改动已验收。修改时间与批次结束时间另存 `external-edits-observed.json`。

复验阶段没有修改或提交生产、测试或工具源码，仅写入原始目录验收记录。本次归档仅提交此 Markdown 报告。原有 `tests/test_browser_chat_followup_gate.py`、`tools/browser_chat_followup_gate.py` 改动保持原样；收尾时新增的其他外部源码修改亦未覆盖或暂存。上次验收与本次首次、重跑结果均保留。
