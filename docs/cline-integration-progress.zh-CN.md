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
2. [完成] Cline 凭证/OAuth/目录/准入/运行链路。
3. [完成] codec/SSE/工具调用/压缩和取消，扩大共享回归。
4. [完成] 设置页认证与思考三态，清理与缺包验收。
5. [完成] 实机测试、相关回归、ruff、三种 Python 类型检查、前端检查与构建。
6. [进行中] 同步用户文档、验收报告，分批提交并 push。

## 已检查

- 初始分支 master，origin 为 waylliam1988/codey；仅用户方案文档未跟踪。
- 仓库无适用 AGENTS.md；现有 Zen 缺包测试使用新进程 import blocker + Local 两协议 + 内核工具/审批 + 历史读取。
- 方案方向成立；需落实跨进程 refresh 互斥与 logout/admission 同门控。

## 验证记录

- 第一批：连接操作、冷启动状态、模型设置、SSE 工具身份与终止取消、请求策略、上下文预算、operator HTTP 鉴权，71 passed。
- 失败根因：重构默认 SSE accumulator 时改变了已有工具错误文案，4 个现有断言失败。属于本次共享层兼容回归，已恢复原诊断，未放宽测试。
- `ruff check codey tests/test_connection_actions.py` 通过；类型检查进行中。
- 在线免费目录：Beam、Nemotron 3.5 Super VL、Nemotron 3.5 Lightning、Solar Mini 4。精确映射 OpenRouter 能力，不采用 Cline SDK 的通用 128K 回退。
- Cline 专属认证/目录/运行链路：56 passed；完整清理与 Cline/Zen 缺包：7 passed；扩大共享协议、压缩、账本与偏好回归：110 passed。
- 实机授权已完成，凭证在 Windows Credential Manager。`.e2e-artifacts/cline-live-20261105-b/summary.json`：Solar Mini 4 JSON 两轮、SSE 两轮、真实 headless chat/read/edit/shell_approve 全部通过。
- 实机根因：Cline 非流式返回 success/data 包装；包内 codec/usage 解包修复，已加入真实形态回归。
- 新增取消竞态测试揭示共享生产缺陷：取消事件在 decode 完成前到达时，原 commit 只检查 generation。现已在提交门检查 `_is_cancelled()`；测试证实拒绝迟到写入。
- 测试假设修正：通用 live gate 原假定所有非 Local provider 都有 Zen 的 `.runtime` 包装，改为通用可选包装解析；另一个新测试漏算请求级时钟消息，改为断言语义历史，保留生产时钟上下文。
- mypy 全生产树 597 文件通过；Pyrefly/ty 指出的 codec 参数名契约和空容器注解已修正，三检查器全部复跑通过，无新增豁免。
- 最终相关 pytest：Cline/通用登录/live gate 91 passed，共享 Local/Zen/历史/压缩/审批/鉴权/打包 214 passed，真实浏览器模型/思考 9 passed，共 314；没有运行全量 pytest。
- 前端 `npm run check`：TypeScript、Biome 和 293 项 Vitest 通过；Vite 构建与随包前端发布/一致性检查通过。
- Ruff `codey tests tools` 全通过。新增费用阻断重启持久化、损坏缓存拒绝准入、清理失败待办、浏览器迟到失败隔离、外部账号版本更新及磁盘 reasoning_details 往返测试。
- 缺包探针仅移除注册并阻止导入，不修改生产逻辑；Local 双协议、Zen、辅助生成、正式独立审查、工具审批、历史、真实 CLI 与 headless 子进程均通过。补审查夹具时漏传 Local 必需 base_url，按现有契约修正测试，未放宽生产入场检查。
- 实机新增：Beam JSON/SSE 两轮和 read；Nemotron Super JSON/SSE 两轮、read/edit；Lightning JSON/SSE 两轮、read，全部通过。
- 当前公布的 17 个思考选项请求均被服务端接受；这不能证明模型内部实际预算执行程度。原 Beam 上游 Invalid request 本轮未复现，不宣称所有任务均已修复。

## 提交与后续接续点

- `30dfd2ce`：通用连接操作、状态/codec/SSE/认证扩展边界与初始方案。
- 下一批提交 Cline 后端、隔离凭证/刷新/清理/缺包测试和实机工具；随后提交前端、随包构建与文档。
- 最后检查脱敏、diff、前端产物一致性后推送 origin/master，核对远端 HEAD；完成前不标记已推送。
