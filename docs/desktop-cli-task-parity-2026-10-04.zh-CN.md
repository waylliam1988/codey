# 桌面与 CLI 任务一致性验收（2026-10-04）

基线 `fe09ecc3`，版本保持 0.5.11。本轮是开发提交，未打 tag、未 Release。
完整验证、首轮失败与最终结果见 [TEST_REPORT](../TEST_REPORT.md)。

## 当前接线

```text
HTTP → derive_entry_auth ─┐
CLI  → run_headless       ├→ TaskSubmission → 既有任务入口/共同内核
         ↓ 同一授权规则 ─┘
桌面 run_task / run_headless → build_task_deps
                               ├→ run_review
                               ├→ run_consensus
                               ├→ run_project_audit
                               └→ run_research_advisors
```

`app/task_services.py` 是应用服务组合的唯一入口；`task/entry_auth.py` 是入口
授权推导的所有者。复用现有 `TaskRunDeps`、正式运行日志、工具循环与完成门，
没有新增另一套调度、持久化或证明系统。

## 用户行为

| 行为 | 当前结果 |
| --- | --- |
| 桌面或 CLI project 修改完成 | 相同策略自动审查，具体问题最多一次 Writer repair |
| 无网页 Reviewer、策略允许自审 | 使用 Writer 的模型，新建隔离审查会话 |
| `require_web` 且无网页 Reviewer | 明示审查不可用，禁止退回自审 |
| 只读请求 | 禁止项目写入/shell，不产生必须修改要求 |
| 明确联网请求或授权 | 同一规则产生网页能力；允许联网与必须打开来源分别处理 |
| 无关联项目 | 不授予项目读取、写入或验证能力 |
| 显式历史审查来源 | 同 session/project、输入/已知模型身份与快照匹配才复用 |
| 复用精确命中 | 从正式存储恢复，不新建 Reviewer 会话、不发送模型请求 |
| 完成要求推导异常 | 抛出异常，不悄悄删除任务要求 |

CLI `agent` 仍默认 `project`；`--auto` 选择自动选路，`--intent` 选择具体任务。
`--readonly`、`--allow-web`、`--allow-write`、`--session-id`、`--continue`、
`--review-source-run-id`、`--review-policy` 进入正式任务流程。
继续会话要求 session；复用要求 session 且只支持 project/review。

展示适配和非交互 shell 默认拒绝仍各有职责。单次 `chat` 命令是 provider 工具；
需要正式任务事实和终态时使用 `agent --intent chat`。

## TDD 与卫生

- 20 条任务行为红测确认默认审查、修复、授权、CLI 参数与错误路径缺口后修绿。
  额外 HTTP 夹具竞态测试先红后绿。八条合法控制/集成用例不冒称先红。
- 八个新文件共 29 条测试，文件名与行为对应。正式桌面/CLI 测试执行真实
  read/edit/run/review/repair；复用测试销毁上下文后只从磁盘恢复。
- 删除 `_no_headless_review`、`_headless_review_for` 和两入口重复依赖组合。
  原有“不审查”默认测试被新的真实 CLI 行为测试取代。
- 只检查日志、来源或身份的旧夹具显式设置“不要求修改”，保留原成功及事实断言。
  搬迁后的源码位置锁迁移到真正所有者；无消费者的转发层未保留。
- 脚本 provider 测试通过 opt-in fixture 隔离真实模型发现，仍走正式服务选择。
  HTTP 测试小请求一次发送，避免晚发 body 与提前拒绝关闭竞态；无重试。

## 验收范围

- 最终全量：**7258 passed、29 skipped、1497 subtests passed，575.36 秒**。
- 机器契约门：**338 passed，70.82 秒，无跳过**。
- Ruff、Mypy（372 个源码文件）、编译、12 个 JavaScript 语法、diff 检查通过。
- 300 次 HTTP 复现：旧夹具 2 次连接中止，新夹具 0 次；两者均 0 次越权状态操作。
- 本轮 KoboldCpp `/models` 不可达，实机 0 attempts / preflight_error。
  没有把上一提交实机成功归算到本轮；未运行真实浏览器布局 E2E。

这些测试证明入口接线和已列出的契约一致，不证明所有代码无 bug，也不证明真实
模型更快或审查质量提高。CLI 接入共同顾问/审查策略后可能增加模型请求和耗时；
应在服务可用时继续用现有发布实机门记录完成率、请求与 token。
