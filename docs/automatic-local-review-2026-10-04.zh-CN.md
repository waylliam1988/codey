# 自动本地自审验收（2026-10-04）

基线 `e4b089da`。版本保持 0.5.11，本轮只提交和推送，不创建 tag/Release。

## 真实接线

```text
local_model_release_gate: project_review
  → run_headless(project, connect_reviewer=固定本地目标)
  → 既有 project Writer / 共同工具内核
  → 既有 project review 阶段 / ReviewCoordinator
  → review_service：新的本地 Reviewer 会话，只读
  → 有具体问题时既有一次 Writer repair
  → 原完成检查 / 持久结果 / 终态
  → 独立文件、测试、真实请求和只读性核验
```

不是先结束 project 再提交一个 review 任务；整个过程共用 run/session。Reviewer
与 Writer 使用同一个固定 endpoint/model，但会话隔离。默认 CLI/headless project
不新增模型调用，嵌入调用显式提供连接器才启用该阶段。桌面在没有网页 Reviewer 且
策略允许自审时，沿用现有本地自审选择。没有新增循环、协议修复预算或复用兜底。

实机 fixture 是没有 `.git` 的普通文件夹，利用现有 tracker/diff 能力；这证明不依赖
该项目的 Git 历史，不等于本轮另做了“系统未安装 Git”的实机环境测试。

## TDD 发现并修复的问题

| 问题 | 修改位置与规则 | 测试 |
| --- | --- | --- |
| Writer 已结算，审查要求修复时直接进入 effect，状态转换非法 | `project_review_phase._repair_writer` 执行前 `mark_writer_running`，之后结算；状态所有者只允许成功 done、无最终证明、更新 attempt 的重新进入 | `test_project_auto_local_review_lifecycle.py`、`test_review_writer_reentry_requires_new_attempt.py` |
| Windows 换行规范化被记成敏感遮蔽 | `reviews/input.py` 用相同规范换行比较真实遮蔽前后内容；不因 CRLF/CR→LF 宣称范围缺失 | `test_review_line_endings_do_not_mark_redaction.py` |
| 项目地图的 `PricingTests.test_discount` 被高熵规则误删 | 仅声明位置且语法完整的合法符号保留；已知密钥格式优先遮蔽，值位置继续筛查 | `test_review_redaction_preserves_declared_symbols.py` |
| 门槛可能把仅 review 或提前终态当自动 project 通过 | 要求真实 Writer/Reviewer 请求，编辑和验证先于 review，终态晚于 review，产物/事件/终态一致 | `test_project_review_gate_requires_real_automatic_flow.py` |

五个文件共 42 条用例；缺陷先红后修，合法控制用例不冒称曾经失败。第一次符号
保留规则偏宽，负例锁定了带 `def` 文字的凭据仍需遮蔽，修正后才进入最终实机和全量。
测试夹具的旧 edit 参数、收据字段和遗漏 emitter 也已修正，未当成生产 bug 汇报。

正式入口测试覆盖 injected connector 与桌面无网页路由两种接线，以及 approved、
actionable finding 后单次 repair、发送结果未知不重发、不生成可复用通过结果、默认
headless 不调用 Reviewer、无关 intent 在建状态前拒绝连接器。

## 结果

- 受影响模块 552 通过、412 子测试通过；最终机器契约门 310 通过且无 skip。
- 全局 ruff、mypy（370 文件零错误）、compileall、JS 语法及 diff 检查通过。
- 最终全量：**7230 通过、29 跳过、1497 子测试通过，零失败，479.99 秒**。
- 最终 KoboldCpp：`review,project_review` 原生 **2/2**、文本 JSON **2/2**。
  模型为 `Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，真实用量、耗时及
  隔离产物路径见 [TEST_REPORT](../TEST_REPORT.md)。首轮实机的符号误遮蔽失败也保留记录。

## 可确认的收益与证明边界

可以确认：自动本地自审已有真实发布门消费者；发现问题后进入持久状态合法的修复；
正常 Windows 文本和项目符号不再误报部分审查。真正的遮蔽仍会标记不完整。
测试和结果存储继续决定完成事实，审查 approved 不替代验证。

状态测试锁定“重新进入 ⇒ 之前成功、无最终证明、attempt 增大”，门槛锁定
“通过 ⇒ 存在同 run 的编码、验证、真实审查、完整持久结果及独立正确文件”。
这些是可执行的有界契约检查；不是整个程序无 bug 的数学证明。

本轮没有跑完整 15-case 实机矩阵或真实浏览器布局 E2E。实机返回 approved，
单次 repair 用脚本化 provider 的正式入口测试锁定；不要求模型偶然给出固定问题。
相同模型的找错质量、总体成功率及大型项目性能没有通过本轮测试证明。mypy 的既有
未注解函数体提示仍存在，也不能称全库每个函数都获得静态类型证明。
