# Cline 集成实施进度

开始：2026-11-05。用户授权实现、相关范围回归、实机免费模型测试、分批提交与推送 GitHub；不运行全量 pytest。

## 约束与决策

- 以 `cline-integration-implementation-plan.zh-CN.md` 为设计基准，实际接口差异在本文记录。
- Cline 生产逻辑只在独立包；共享层通过注册、可选认证/codec/stream 扩展调用。
- Windows 凭证安全存储，不明文回退；测试注入独立存储，不写生产账号。
- 真实免费目录与模型能力逐项核对；未验证能力不虚构思考档位。
- 根因分类区分生产缺陷、旧测试假设和环境/上游限制。

## 阶段

1. [完成] 核对仓库和 Cline 协议、通用连接操作与状态生命周期。
2. [进行中] Cline 凭证/OAuth/目录/准入/运行链路。
3. [待办] codec/SSE/工具调用/压缩和取消。
4. [待办] 设置页认证与思考三态，清理与缺包验收。
5. [待办] 实机测试、扩大相关回归、ruff、三种 Python 类型检查、前端检查与构建。
6. [待办] 更新用户文档、验收报告，提交并 push。

## 已检查

- 初始分支 master，origin 为 waylliam1988/codey；仅用户方案文档未跟踪。
- 仓库无适用 AGENTS.md；现有 Zen 缺包测试使用新进程 import blocker + Local 两协议 + 内核工具/审批 + 历史读取。
- 方案方向成立；需落实跨进程 refresh 互斥与 logout/admission 同门控。

## 验证记录

- 第一批：连接操作、冷启动状态、模型设置、SSE 工具身份与终止取消、请求策略、上下文预算、operator HTTP 鉴权，71 passed。
- 失败根因：重构默认 SSE accumulator 时改变了已有工具错误文案，4 个现有断言失败。属于本次共享层兼容回归，已恢复原诊断，未放宽测试。
- `ruff check codey tests/test_connection_actions.py` 通过；类型检查进行中。
- 在线免费目录：Beam、Nemotron 3.5 Super VL、Nemotron 3.5 Lightning、Solar Mini 4。精确映射 OpenRouter 能力，不采用 Cline SDK 的通用 128K 回退。
